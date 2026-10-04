#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""agent-mail (mesh v1) — boîte aux lettres des agents, sur Postgres.

  agent-mail send <dest> <texte…> [--from NOM] [--lot ID]
                                                 dépose un message (dest = nom, ou "all")
                                                 et l'écrit dans le fil lisible du projet
                                                 ou du lot (ameesh fil show <projet> [<lot>])
  agent-mail list                                agents, hôte, bail, non lus
  agent-mail inbox [NOM]                         messages non lus de NOM (sans les marquer lus)
  agent-mail whoami                              identité liée ; --cwd = diagnostic
  agent-mail alias <NOM> <DOSSIER> [CHANTIER]    nomme les sessions dont le dossier commence par DOSSIER
  agent-mail status "<travail en cours>"         état de l'agent (registre + titre du terminal)
  agent-mail hook <claude|codex|deepseek>        hook : lit le JSON sur stdin, livre les non-lus
  agent-mail statusline                          barre d'état Claude Code : [chantier/nom] travail · dossier
  agent-mail migrate                             applique les migrations versionnées
  agent-mail doctor [--notify-test]              diagnostic : pilote, schéma, migrations, LISTEN/NOTIFY

Identité d'une session : $AGENT_MAIL_NAME (le runner la pose pour le harnais
qu'il lance). Si $AMEESH_RUNNER_ID et $AMEESH_LEASE_EPOCH sont aussi posées, le
bail doit être vivant et détenu par ce runner — l'identité est liée au bail.
Le dossier courant ne donne JAMAIS d'identité : lire dans le worktree d'un
autre agent ne consomme pas son courrier. Les anciens noms AGENT_MESH_* restent
acceptés en alias.

Backend : Postgres (AMEESH_DSN, schéma AMEESH_SCHEMA). Si la base est
absente et que AMEESH_BACKEND=auto, repli lisible sur les fichiers v0
(~/.local/state/agent-mail) : mêmes commandes, mêmes JSON de hook, rien ne casse.
Les hooks ne font jamais échouer l'agent : toute erreur → sortie 0 sans rien.
Un message d'agent n'a jamais l'autorité du propriétaire : l'expéditeur est affiché.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import authority
from . import backend as backend_mod
from . import config as config_mod
from . import db as db_mod
from . import fil, identity, migrations, signing, storage
from .config import NAME_RE, Config

MAX_STOP_BLOCKS = 3

#: verdict neutre pour le comptage : un message non signé n'a aucune autorité
_NO_PROOF = authority.Verdict(False, "non signé")


# --------------------------------------------------------------------------
# rendu (identique à la v0 : les hooks doivent produire le même JSON)
# --------------------------------------------------------------------------

def verdicts_for(bk, msgs: list[dict]) -> dict:
    """Vérifie les messages signés (R4). Le repli fichier n'a pas de signature."""
    if getattr(bk, "kind", None) != "pg":
        return {}
    out: dict = {}
    for msg in msgs:
        if not msg.get("signature"):
            continue
        try:
            out[msg.get("id")] = authority.verify_message(bk.db, msg)
        except Exception as exc:  # une vérification impossible n'accorde rien
            out[msg.get("id")] = authority.Verdict(False, "vérification impossible : %s" % exc)
    return out


def render(msgs: list[dict], verdicts: dict | None = None) -> str:
    """Le texte injecté par les hooks. Sans signature, il est identique à la v0.

    Trois états distincts, parce qu'une signature d'agent n'est pas l'autorité
    du propriétaire : non signé (message d'agent, comme en v0), signé par un
    agent (authentique, sans autorité), signé par le propriétaire (autorité
    prouvée) — plus le cas signature invalide ou expirée, toujours signalé.
    """
    verdicts = verdicts or {}
    prouves = sum(1 for msg in msgs
                  if (verdicts.get(msg.get("id")) or _NO_PROOF).authority)
    agents_signes = sum(1 for msg in msgs
                        if getattr(verdicts.get(msg.get("id")), "authentic", False)
                        and verdicts[msg.get("id")].role == "agent")
    if prouves:
        lines = [
            "[agent-mail] %d nouveau(x) message(s) — dont %d avec l'autorité du "
            "propriétaire PROUVÉE (signature Ed25519)%s ; les autres n'ont pas cette "
            "autorité :" % (
                len(msgs), prouves,
                " et %d signé(s) par un agent (authentique(s), sans autorité "
                "propriétaire)" % agents_signes if agents_signes else "")
        ]
    elif agents_signes:
        lines = [
            "[agent-mail] %d nouveau(x) message(s) — dont %d signé(s) par un agent "
            "(authentique(s), sans autorité du propriétaire) ; les autres n'ont pas "
            "cette autorité :" % (len(msgs), agents_signes)
        ]
    else:
        lines = [
            "[agent-mail] %d nouveau(x) message(s) — l'expéditeur est indiqué ; "
            "un message d'agent n'a pas l'autorité du propriétaire :" % len(msgs)
        ]
    for msg in msgs:
        moment = time.strftime("%H:%M", time.localtime(msg.get("ts", 0)))
        verdict = verdicts.get(msg.get("id"))
        suffixe = ""
        if verdict is not None:
            expire = time.strftime("%H:%M", time.localtime(verdict.expires_ts)) \
                if verdict.expires_ts else "?"
            if verdict.authority:
                suffixe = "  [autorité du propriétaire PROUVÉE, expire %s]" % expire
            elif verdict.ok:
                suffixe = ("  [signé par un agent, authentique, sans autorité "
                           "propriétaire, expire %s]" % expire)
            else:
                suffixe = "  [⚠ signature NON valide : %s]" % verdict.reason
        lines.append("— de %s à %s : %s%s" % (
            msg.get("from", "?"), moment, msg.get("text", ""), suffixe))
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
    key_path = None
    expires = None
    lot = None
    kind = "notify"
    urgent = False
    args = list(args)
    for flag in ("--from", "--key", "--expires", "--lot", "--kind"):
        if flag in args:
            index = args.index(flag)
            value = args[index + 1] if index + 1 < len(args) else None
            args = args[:index] + args[index + 2:]
            if flag == "--from":
                sender = value
            elif flag == "--key":
                key_path = value
            elif flag == "--expires":
                expires = value
            elif flag == "--lot":
                lot = value
            else:
                kind = value
    if "--urgent" in args:
        urgent = True
        args = [arg for arg in args if arg != "--urgent"]
    if kind not in ("request", "reply", "notify", "event"):
        print("kind invalide : %r (request|reply|notify|event)" % (kind,), file=sys.stderr)
        return 2
    if urgent and kind != "event":
        print("--urgent n'a de sens qu'avec --kind event", file=sys.stderr)
        return 2
    sign = key_path is not None or "--sign" in args
    allow_structured = "--allow-structured" in args
    args = [arg for arg in args if arg not in ("--sign", "--allow-structured")]
    if expires and not sign:
        print("--expires n'a de sens qu'avec --sign/--key", file=sys.stderr)
        return 2
    if len(args) < 2:
        print("usage: agent-mail send <dest|all> <texte…> [--from NOM] [--lot ID] "
              "[--kind request|reply|notify|event] [--urgent] "
              "[--sign --key FICHIER] [--expires 24h]", file=sys.stderr)
        return 2
    if lot is not None and not NAME_RE.match(lot):
        print("lot invalide : %r (lettres, chiffres, . _ -)" % lot, file=sys.stderr)
        return 2
    dest, text = args[0], " ".join(args[1:]).strip()
    if not sender:
        binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
        if not binding.ok:
            print("expéditeur non lié : posez AGENT_MAIL_NAME (le runner le fait) "
                  "ou passez --from NOM", file=sys.stderr)
            return 2
        sender = binding.name
    if not NAME_RE.match(sender) or not text:
        print("expéditeur ou texte invalide", file=sys.stderr)
        return 2
    if dest != "all" and not NAME_RE.match(dest):
        print("destinataire invalide", file=sys.stderr)
        return 2
    # R12 : le fil est lu par des humains ; un corps illisible n'est pas déposé.
    if not allow_structured:
        reason = fil.unreadable_reason(text)
        if reason:
            print("message refusé — %s\n(--allow-structured est réservé aux tests et "
                  "aux outils)" % reason, file=sys.stderr)
            return 2

    signed = None
    if sign:
        if not key_path:
            print("--sign exige --key <clé privée> (acte du propriétaire)", file=sys.stderr)
            return 2
        if dest == "all":
            print("un message signé vise un destinataire précis (pas « all »)", file=sys.stderr)
            return 2
        try:
            ttl = authority.parse_ttl(expires)
            seed = signing.read_private(os.path.expanduser(key_path))
            signed = authority.sign_message(
                seed, sender=sender, recipient=dest, body=text, ttl=ttl)
        except (ValueError, authority.AuthorityError, OSError) as exc:
            print("signature impossible : %s" % exc, file=sys.stderr)
            return 2
        if bk.kind == "pg":
            info = authority.key_info(bk.db, sender)
            if not info or not info.get("public_key"):
                print("attention : aucune clé enregistrée pour %s — le message ne sera "
                      "pas reconnu comme signé" % sender, file=sys.stderr)
            elif info.get("public_key_fingerprint") != signed["signature_key"]:
                print("attention : l'empreinte %s n'est pas celle enregistrée pour %s (%s) — "
                      "le message ne sera pas reconnu" % (
                          signed["signature_key"][:12], sender,
                          (info.get("public_key_fingerprint") or "?")[:12]), file=sys.stderr)
        else:
            print("attention : en repli fichier, la signature n'est pas conservée",
                  file=sys.stderr)

    targets = bk.send(sender, dest, text, host=cfg.host, signed=signed,
                      work_item_id=lot, allow_structured=allow_structured,
                      kind=kind, urgent=urgent)
    if not targets:
        print("aucun destinataire")
    elif signed:
        print("déposé pour : %s (signé %s, expire %s)" % (
            ", ".join(targets), signed["signature_key"][:12],
            time.strftime("%H:%M", time.localtime(signed["expires_us"] / 1_000_000))))
    else:
        print("déposé pour : " + ", ".join(targets))
    return 0


def cmd_inbox(bk, cfg: Config, rest: list[str]) -> int:
    if rest:
        name = rest[0]  # lire la boîte d'un autre n'accorde rien et ne consomme rien
    else:
        binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
        if not binding.ok:
            print("identité non liée : %s" % (binding.reason or "AGENT_MAIL_NAME absente"),
                  file=sys.stderr)
            return 1
        name = binding.name
    msgs = bk.unread(name)
    verdicts = verdicts_for(bk, msgs)
    for msg in msgs:
        verdict = verdicts.get(msg.get("id"))
        marque = ""
        if verdict is not None:
            if verdict.authority:
                marque = "  [signé par le propriétaire]"
            elif verdict.ok:
                marque = "  [signé par un agent]"
            else:
                marque = "  [⚠ signature NON valide : %s]" % verdict.reason
        print("de %s%s : %s" % (msg.get("from"), marque, msg.get("text")))
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
    binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
    if not binding.ok:
        print("identité non liée : %s" % (binding.reason or "AGENT_MAIL_NAME absente"),
              file=sys.stderr)
        return 1
    name = binding.name
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
    try:
        _bk, _warn = backend_mod.open_backend(cfg)
    except db_mod.DbError:
        _bk = backend_mod.FileBackend(cfg)
    # Affichage seulement : l'identité liée si elle existe, sinon le dossier
    # (diagnostic). Rien n'est consommé, aucune autorité n'est accordée.
    binding = identity.resolve_binding(cfg, _bk.db if _bk.kind == "pg" else None)
    name = binding.name if binding.ok else identity.legacy_name(cwd, cfg)
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


def _emit(payload: dict) -> bool:
    """Écrit le JSON du hook. Renvoie False si l'écriture échoue.

    L'appelant ne marque les messages remis QU'APRÈS une écriture réussie :
    un harnais qui ferme son entrée (BrokenPipe) ne doit pas faire perdre du
    courrier déjà consommé.
    """
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False


def cmd_hook(cfg: Config, tool: str) -> int:
    """Hook de harnais : lit le JSON sur stdin, ne fait JAMAIS échouer l'agent."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    event = data.get("hook_event_name") or ""
    try:
        bk, _warning = backend_mod.open_backend(cfg)
    except Exception:
        bk = backend_mod.FileBackend(cfg)
    # L'identité vient de l'environnement (le runner la pose), jamais du dossier :
    # lire la doc d'un autre agent dans son worktree ne consomme plus son courrier.
    binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
    if not binding.ok:
        if binding.name:
            # Identité annoncée mais bail invalide : on le dit sur stderr, et
            # on ne touche à rien.
            print("agent-mail : identité %s non liée (%s) — courrier laissé en place"
                  % (binding.name, binding.reason), file=sys.stderr)
        return 0
    name = binding.name
    try:
        bk.register(name, tool, data.get("cwd"), data.get("session_id"))
        set_title(cfg, bk, name)
        msgs = bk.unread(name)
        verdicts = verdicts_for(bk, msgs)
        if event == "Stop":
            if not msgs:
                bk.stop_counter(name, reset=True)
                return 0
            # stop_hook_active n'est pas fiable selon les harnais : on compte
            # nous-mêmes les tours forcés d'affilée (comme la v0).
            if bk.stop_counter(name) >= MAX_STOP_BLOCKS:
                return 0
            # Écrire d'abord : si le harnais a fermé son entrée, on ne consomme
            # pas les messages pour autant.
            if not _emit({"decision": "block", "reason": render(msgs, verdicts)}):
                return 0
            bk.stop_counter(name, bump=True)
            bk.mark_read(name, msgs)
            return 0
        if event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
            if event == "UserPromptSubmit":
                bk.stop_counter(name, reset=True)
            if not msgs:
                return 0
            if not _emit({
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": render(msgs, verdicts),
                }
            }):
                return 0
            bk.mark_read(name, msgs)
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
    agents = storage.of(db).agents.count()
    unread = storage.of(db).mailbox.unread_total()
    print("agents     : %d" % agents)
    print("non lus    : %d" % unread)
    verdict = 0
    if notify_test:
        channel = "ameesh_doctor"
        listener = storage.of(db).wakeups.subscribe([channel])
        if listener is None:
            print("notify     : pilote sans LISTEN/NOTIFY")
            verdict = 1
        else:
            time.sleep(0.3)
            storage.of(db).wakeups.notify(channel, "ping")
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
    if not argv or argv[0] in ("-h", "--help"):
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
            if len(rest) > 1 and rest[0] == "--cwd":
                # Diagnostic : le dossier ne donne aucune identité.
                print("legacy %s (diagnostic, non autoritaire)"
                      % identity.legacy_name(rest[1], cfg))
                return 0
            binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
            if not binding.ok:
                print("identité non liée : %s" % (binding.reason or "AGENT_MAIL_NAME absente"),
                      file=sys.stderr)
                return 1
            print(binding.name if binding.source == "explicit"
                  else "%s [%s]" % (binding.name, binding.source))
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
