#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""agent-mail (mesh v1) — the agents' mailbox, on Postgres.

  agent-mail send <dest> <text…> [--from NAME] [--lot ID|REF [--assign]] [--new-lot "title"
                 [--estimate 2h] [--app PROJECT]] [--force REASON] [--queue]
                                                 drops a message (dest = name, or "all")
                                                 and writes it to the readable thread of the
                                                 project or work item (ameesh fil show <project> [<lot>]);
                                                 from a declared orchestrator, --lot hands an
                                                 unassigned work item (or one held by a human)
                                                 to the recipient — an item already held by an
                                                 agent is never reassigned by mail, whoever
                                                 holds it (#1066); --assign is the explicit
                                                 gesture that lets any sender hand over an
                                                 unassigned item (#964) — and --new-lot creates it (L118),
                                                 with its estimated duration --estimate (L157:
                                                 required when an agent creates the work item);
                                                 #540: a recipient who already has a work item
                                                 not started does not get it (team queue, the
                                                 message is not dropped) unless --force "reason";
                                                 refused to a name not in the registry (close
                                                 names suggested), to a stopped agent
                                                 (unless --queue) and from a stopped identity
                       [--urgent] [--ack] [--cc NAME[,NAME…]]
                                                 L125: an idle agent is woken only
                                                 after a batching window
                                                 (AMEESH_MAIL_BATCH, 90 s); --urgent wakes
                                                 it at once; --ack (acknowledgement)
                                                 and --cc (copy, as an event)
                                                 do not wake: read on the next turn;
                                                 nor does "all" without --urgent
  agent-mail forward <old> <new> [--dry-run] [--json]
                                                 re-delivers the pending mail of a stopped
                                                 agent (or one missing from the registry) to a
                                                 live agent: sender and date kept
  agent-mail sweep [--days N] [--dry-run] [--json]
                                                 L976: re-delivers the dead mail of a stopped
                                                 agent to its successor, expires the rest past
                                                 `mail_dead_letter_days` (7; 0 = never), with
                                                 a thread trace; run daily by the runner
  agent-mail list                                agents, host, lease, unread
  agent-mail inbox [NAME]                        unread messages of NAME (without marking them read)
  agent-mail whoami [--session ID --harness H]   bound identity and its source (runner, explicit,
                                                 session); --cwd DIR = diagnostic
  agent-mail bind <NAME> --session ID --harness claude|codex|deepseek [--pid N] [--force]
                                                 binds an EXTERNAL session (harness session
                                                 identifier) to NAME, on this host (L41, 0030);
                                                 `execute` agent refused without --force (L46)
  agent-mail bind --import FILE                  imports the bindings of the local bridge
                                                 (external-session-bindings.json)
  agent-mail unbind --session ID --harness H     revokes the binding of this session
  agent-mail bindings [--all] [--json]           active session bindings (--all: revoked ones too)
  agent-mail alias <NAME> <DIR> [WORKSPACE]      names the sessions whose directory starts with DIR
  agent-mail status "<current work>"             agent status (registry + terminal title)
  agent-mail hook <claude|codex|deepseek>        hook: reads the JSON on stdin, delivers the unread
  agent-mail statusline                          Claude Code status bar: [workspace/name] work · directory
  agent-mail migrate [--dry-run | --additive-only]  applies the versioned migrations
  agent-mail doctor [--notify-test | --probe | --harness]  diagnostics: driver, schema, migrations, LISTEN/NOTIFY and the OS desktop channel (L66); host harnesses (L106)
                                                 (--probe: lightweight container probe)

Identity of a session: $AGENT_MAIL_NAME (the runner sets it for the harness
it launches). If $AMEESH_RUNNER_ID and $AMEESH_LEASE_EPOCH are also set, the
lease must be live and held by that runner — the identity is bound to the lease.
Without $AGENT_MAIL_NAME, an EXTERNAL session (launched by a human) has an
identity only if it is BOUND (`bind`, source "session"): the hook finds it
by the session identifier the harness passes to it and, if the
binding carries a --pid, requires that PID to be an ancestor of the hook; a command
run inside the session (whoami, send, inbox) finds it by its ancestry.
Without an identity, the hook delivers NOTHING and writes nothing. A bound session
never replaces an agent's registered session, and an agent holding a
live lease cannot be bound (it is driven by the runner).
The current directory NEVER gives an identity: reading in another agent's
worktree does not consume its mail. The old AGENT_MESH_* names remain
accepted as aliases.

Backend: Postgres (AMEESH_DSN, schema AMEESH_SCHEMA). If the database is
missing and AMEESH_BACKEND=auto, readable fallback to the v0 files
(~/.local/state/agent-mail): same commands, same hook JSON, nothing breaks.
Hooks never make the agent fail: any error → exit 0 with no output.
An agent's message never has the owner's authority: the sender is shown.
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
from . import fastmail
from . import fil, identity, mail, migrations, platform, registry, signing, storage
from . import session_bindings as sb
from .config import NAME_RE, Config

MAX_STOP_BLOCKS = 3
#: L61 : un hook ne réinscrit pas l'agent (aller-retour vers la base) plus
#: d'une fois par cette durée, tant que harnais, dossier, session et bail
#: n'ont pas changé — `last_seen` avance alors par pas d'au plus une minute.
REGISTER_EVERY_S = 60.0

#: verdict neutre pour le comptage : un message non signé n'a aucune autorité
_NO_PROOF = authority.Verdict(False, "unsigned")


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
            out[msg.get("id")] = authority.Verdict(False, "verification failed: %s" % exc)
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
            "[agent-mail] %d new message(s) — %d of them with the owner's authority "
            "PROVEN (Ed25519 signature)%s; the others do not have this "
            "authority:" % (
                len(msgs), prouves,
                " and %d signed by an agent (authentic, no owner "
                "authority)" % agents_signes if agents_signes else "")
        ]
    elif agents_signes:
        lines = [
            "[agent-mail] %d new message(s) — %d of them signed by an agent "
            "(authentic, without the owner's authority); the others do not have "
            "this authority:" % (len(msgs), agents_signes)
        ]
    else:
        lines = [
            "[agent-mail] %d new message(s) — the sender is shown; "
            "an agent's message does not have the owner's authority:" % len(msgs)
        ]
    for msg in msgs:
        moment = time.strftime("%H:%M", time.localtime(msg.get("ts", 0)))
        verdict = verdicts.get(msg.get("id"))
        suffixe = ""
        if verdict is not None:
            expire = time.strftime("%H:%M", time.localtime(verdict.expires_ts)) \
                if verdict.expires_ts else "?"
            if verdict.authority:
                suffixe = "  [owner's authority PROVEN, expires %s]" % expire
            elif verdict.ok:
                suffixe = ("  [signed by an agent, authentic, no owner "
                           "authority, expires %s]" % expire)
            else:
                suffixe = "  [⚠ signature NOT valid: %s]" % verdict.reason
        if msg.get("deja_consigne"):
            suffixe += "  [re-delivered after an outage: may already have been handled]"
        suffixe += mail.forwarded_note(msg)
        # L125 : un message passif (copie, accusé, diffusion) le dit ; un
        # message ordinaire garde la ligne de la v0, traduite (L156c)
        passif = mail.passive_label(msg)
        lines.append("— from %s at %s%s: %s%s" % (
            msg.get("from", "?"), moment, " (%s)" % passif if passif else "",
            msg.get("text", ""), suffixe))
    lines.append("(to reply: agent-mail send <name> \"…\")")
    return "\n".join(lines)


def find_tty() -> str | None:
    """Le terminal de l'humain (couche plateforme, L63), ou None."""
    return platform.human_tty()


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
    new_lot = None
    parent_lot = None
    app = None
    kind = "notify"
    urgent = False
    args = list(args)
    # L125 : copies sans réveil (--cc NOM[,NOM…], répétable) et accusé (--ack)
    cc: list[str] = []
    while "--cc" in args:
        index = args.index("--cc")
        value = args[index + 1] if index + 1 < len(args) else ""
        args = args[:index] + args[index + 2:]
        if not value.strip():
            print("--cc expects an agent name (or several, comma-separated)",
                  file=sys.stderr)
            return 2
        cc += [name.strip() for name in value.split(",") if name.strip()]
    ack = "--ack" in args
    args = [arg for arg in args if arg != "--ack"]
    # #964: `--assign` is the explicit gesture that lets a `--lot` assign a
    # FREE lot (or one held by a human) to the recipient, even outside the
    # orchestrator detection (L94 history included); never a lot held by an
    # agent (#1066).
    assign = "--assign" in args
    args = [arg for arg in args if arg != "--assign"]
    # #506: --app may name a project nobody declares yet, on purpose
    new_project = "--new-project" in args
    args = [arg for arg in args if arg != "--new-project"]
    # L514: a review verdict or an arbitration reaches a dsh or codex turn
    # at its next safe point instead of waiting for the end of the turn
    marks = [key for key in mail.MID_TURN_KEYS if "--" + key in args]
    args = [arg for arg in args if arg not in ["--" + key for key in mail.MID_TURN_KEYS]]
    explicit_sender = "--from" in args
    estimate = None
    force = None
    for flag in ("--from", "--key", "--expires", "--lot", "--new-lot", "--parent-lot",
                 "--kind", "--estimate", "--app", "--force"):
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
            elif flag == "--new-lot":
                new_lot = value if value is not None else ""
            elif flag == "--parent-lot":
                parent_lot = value if value is not None else ""
            elif flag == "--estimate":
                estimate = value if value is not None else ""
            elif flag == "--app":
                app = value if value is not None else ""
            elif flag == "--force":
                # #540 : forcer la file d'équipe, avec sa raison (journal du lot)
                force = value if value is not None else ""
            else:
                kind = value
    if "--urgent" in args:
        urgent = True
        args = [arg for arg in args if arg != "--urgent"]
    # dépôt explicite chez un agent arrêté (lu à sa reprise, ou renvoyé)
    queue = "--queue" in args
    args = [arg for arg in args if arg != "--queue"]
    explicit = sender is not None
    if kind not in ("request", "reply", "notify", "event"):
        print("invalid kind: %r (request|reply|notify|event)" % (kind,), file=sys.stderr)
        return 2
    # L125 : --urgent vaut pour tout message (réveil sans délai) ; il
    # n'interrompt un tour en cours que d'un expéditeur habilité (0018)
    if ack and urgent:
        print("--ack and --urgent contradict each other: an acknowledgement does not wake",
              file=sys.stderr)
        return 2
    if ack and marks:
        print("--ack and --%s contradict each other: an acknowledgement does not "
              "interrupt a turn" % marks[0], file=sys.stderr)
        return 2
    if ack and new_lot is not None:
        print("--ack and --new-lot contradict each other: an acknowledgement does not hand over work",
              file=sys.stderr)
        return 2
    if ack and assign:
        print("--ack and --assign contradict each other: an acknowledgement does not hand over work",
              file=sys.stderr)
        return 2
    sign = key_path is not None or "--sign" in args
    allow_structured = "--allow-structured" in args
    args = [arg for arg in args if arg not in ("--sign", "--allow-structured")]
    if expires and not sign:
        print("--expires only makes sense with --sign/--key", file=sys.stderr)
        return 2
    if len(args) < 2:
        print("usage: agent-mail send <dest|all> <text…> [--from NAME] [--lot ID|REF [--assign]] "
              "[--new-lot \"title\" [--estimate 2h] [--app PROJECT [--new-project]] "
              "[--parent-lot ID]] "
              "[--kind request|reply|notify|event] "
              "[--urgent] [--verdict|--arbitration] "
              "[--ack] [--cc NAME[,NAME…]] [--sign --key FILE] [--expires 24h] [--queue]",
              file=sys.stderr)
        return 2
    if lot is not None and not NAME_RE.match(lot):
        print("invalid lot: %r (letters, digits, . _ -)" % lot, file=sys.stderr)
        return 2
    if new_lot is not None and lot is not None:
        print("--lot OR --new-lot, not both", file=sys.stderr)
        return 2
    if assign and lot is None:
        print("--assign requires --lot <id|ref>: say which lot to assign to the recipient",
              file=sys.stderr)
        return 2
    if assign and new_lot is not None:
        print("--assign goes with --lot (--new-lot already assigns the created lot to the "
              "recipient)", file=sys.stderr)
        return 2
    if new_lot is not None and not new_lot.strip():
        print("--new-lot: empty title", file=sys.stderr)
        return 2
    if app is not None and new_lot is None:
        print("--app goes with --new-lot (to correct an existing lot: "
              "ameesh work set-app <id> <project>)", file=sys.stderr)
        return 2
    if app is not None and not app.strip():
        print("--app: empty project", file=sys.stderr)
        return 2
    if new_project and app is None:
        print("--new-project goes with --new-lot \"title\" --app <project>", file=sys.stderr)
        return 2
    if estimate is not None and new_lot is None:
        print("--estimate goes with --new-lot (the estimated duration of the created work item; for an "
              "existing one: ameesh work plan <id> --estimate 2h)", file=sys.stderr)
        return 2
    if parent_lot is not None and new_lot is None:
        print("--parent-lot goes with --new-lot (the parent of the created suite; for an "
              "existing lot: ameesh work dependencies <id> --add <parent>)", file=sys.stderr)
        return 2
    if parent_lot is not None:
        number = str(parent_lot).strip().lstrip("#")
        if not number.isdigit() or int(number) <= 0:
            print("--parent-lot: a lot number (e.g. 1070 or #1070)", file=sys.stderr)
            return 2
        parent_lot = int(number)
    dest, text = args[0], " ".join(args[1:]).strip()
    binding = None
    if not sender:
        binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
        if not binding.ok:
            # la raison quand une identité est annoncée (ex. « agent arrêté »)
            print("unbound sender%s: set AGENT_MAIL_NAME (the runner does it) "
                  "or pass --from NAME" % (
                      " (%s: %s)" % (binding.name, binding.reason)
                      if binding.name and binding.reason else ""), file=sys.stderr)
            return 2
        sender = binding.name
    if not NAME_RE.match(sender) or not text:
        print("invalid sender or text", file=sys.stderr)
        return 2
    if "," in dest:
        # L125 : un seul destinataire réveillé ; les autres en copie
        print("one recipient per message: to copy the others "
              "without waking them, --cc %s" % ",".join(
                  n.strip() for n in dest.split(",")[1:] if n.strip()), file=sys.stderr)
        return 2
    if dest != "all" and not NAME_RE.match(dest):
        print("invalid recipient", file=sys.stderr)
        return 2
    # L125 : les copies — ni au destinataire, ni à soi, sans doublon ; un
    # agent inconnu est refusé avant tout dépôt
    cc = [n for i, n in enumerate(cc) if n not in (dest, sender) and n not in cc[:i]]
    if cc and (dest == "all" or sign):
        print("--cc applies to an unsigned message to a single recipient", file=sys.stderr)
        return 2
    for name in cc:
        if not NAME_RE.match(name):
            print("copy: invalid agent name %r" % name, file=sys.stderr)
            return 2
        if bk.kind == "pg":
            from . import undeliverable
            if undeliverable.registry_rows(bk.db, [name])[name] is None:
                print("copy refused: unknown agent: %s (nothing delivered)" % name,
                      file=sys.stderr)
                return 2
    # R12 : le fil est lu par des humains ; un corps illisible n'est pas déposé.
    if not allow_structured:
        reason = fil.unreadable_reason(text)
        if reason:
            print("message refused — %s\n(--allow-structured is reserved for tests and "
                  "tools)" % reason, file=sys.stderr)
            return 2

    signed = None
    if sign:
        if not key_path:
            print("--sign requires --key <private key> (owner act)", file=sys.stderr)
            return 2
        if dest == "all":
            print("a signed message targets a single recipient (not \"all\")", file=sys.stderr)
            return 2
        try:
            ttl = authority.parse_ttl(expires)
            seed = signing.read_private(os.path.expanduser(key_path))
            signed = authority.sign_message(
                seed, sender=sender, recipient=dest, body=text, ttl=ttl)
        except (ValueError, authority.AuthorityError, OSError) as exc:
            print("cannot sign: %s" % exc, file=sys.stderr)
            return 2
        if bk.kind == "pg":
            info = authority.key_info(bk.db, sender)
            if not info or not info.get("public_key"):
                print("warning: no key registered for %s — the message will "
                      "not be recognized as signed" % sender, file=sys.stderr)
            elif info.get("public_key_fingerprint") != signed["signature_key"]:
                print("warning: fingerprint %s is not the one registered for %s (%s) — "
                      "the message will not be recognized" % (
                          signed["signature_key"][:12], sender,
                          (info.get("public_key_fingerprint") or "?")[:12]), file=sys.stderr)
        else:
            print("warning: in file fallback, the signature is not kept",
                  file=sys.stderr)

    # Courrier en souffrance (2026-10-11) : ni agent fantôme, ni boîte morte,
    # ni réponse vers une identité arrêtée — refusé AVANT tout dépôt (et avant
    # qu'un lot soit créé ou rattaché). Le repli fichier n'a pas de registre.
    if bk.kind == "pg":
        from . import undeliverable
        try:
            notes = undeliverable.check_send(cfg, bk.db, sender=sender, dest=dest, queue=queue,
                                             explicit=explicit, binding=binding)
        except undeliverable.Refused as exc:
            print("message not delivered — %s" % exc, file=sys.stderr)
            return 2
        for note in notes:
            print("warning: %s" % note, file=sys.stderr)

    # L125 : ce qui réveille le destinataire. Un humain réveille tout de suite
    # (jamais d'accusé reconnu pour lui) ; un accusé de réception — `--ack`, ou
    # un texte très court qui n'est QUE cela (`mail.looks_like_ack`) — attend
    # son prochain tour sans le réveiller.
    human = _sender_is_human(bk, cfg, sender, explicit_sender)
    payload: dict = {"human": True} if human else {}
    payload.update((key, True) for key in marks)
    if ack or (not urgent and not sign and not human and not marks and dest != "all"
               and lot is None and new_lot is None and kind in ("notify", "reply")
               and mail.looks_like_ack(text)):
        payload["ack"] = True

    # L118 : le courrier qui confie du travail est lié à un lot (avant le
    # dépôt : une attribution refusée ne dépose rien)
    thread_meta = None
    if new_lot is not None and getattr(bk, "mediated", False):
        print("--new-lot is unavailable in mediated contract 1.1; create and assign "
              "the work item on the server before sending mail", file=sys.stderr)
        return 2
    if new_lot is not None and (bk.kind != "pg" or dest == "all"):
        print("--new-lot requires the database and a single recipient", file=sys.stderr)
        return 2
    if assign and getattr(bk, "mediated", False):
        print("--assign is unavailable in mediated contract 1.1; assign the work item "
              "on the server before sending mail", file=sys.stderr)
        return 2
    if assign and (bk.kind != "pg" or dest == "all"):
        print("--assign requires the database and a single recipient", file=sys.stderr)
        return 2
    # Contract 1.1 permits a thread label, but not assignment or work creation.
    # The server remains responsible for linking and assigning work items.
    if bk.kind == "pg" and dest != "all" and not getattr(bk, "mediated", False):
        from . import assignments, plan_git, work
        try:
            if (force or "").strip():
                # #540 : l'auteur du forçage est l'identité de la session
                sender = work.forcer(cfg, bk.db, sender)
            linked = assignments.link_message(
                cfg, bk.db, sender=sender, recipient=dest, lot=lot, new_lot=new_lot,
                branch=plan_git.cited_branch(text) if (lot or new_lot is not None) else None,
                estimate=estimate, app=app, new_project=new_project, assign=assign,
                force=force,
                parent_lot=parent_lot)
        except (assignments.AssignmentError, work.WorkError) as exc:
            print("message not delivered — %s" % exc, file=sys.stderr)
            return 2
        lot = linked["work_item_id"]
        for warning in linked["warnings"]:
            print("warning: %s" % warning, file=sys.stderr)
        if linked["warnings"]:
            thread_meta = {"avertissement": "; ".join(linked["warnings"])}
        item = linked["lot"]
        if item is not None and (linked["created"] or linked["assigned"] or linked["branch"]):
            # #858 : un lot créé pour un destinataire non attribuable à
            # l'instant est créé sans assigné (l'avertissement dit pourquoi).
            if linked["created"]:
                quoi = ("created and assigned to %s" % dest if linked["assigned"]
                        else "created for %s without assignee" % dest)
            else:
                quoi = "assigned to %s" % dest if linked["assigned"] else "linked"
            print("lot #%d %s%s%s" % (
                int(item["id"]), quoi,
                ": %s" % item.get("title") if linked["created"] else "",
                " (branch %s)" % linked["branch"] if linked["branch"] else ""))
        if payload.get("ack") and (linked["created"] or linked["assigned"]):
            # L125 : un message qui confie un lot n'est jamais un accusé
            del payload["ack"]
            print("warning: this message hands a work item to %s: it wakes it "
                  "(not an acknowledgement)" % dest, file=sys.stderr)

    skipped: list[str] = []
    targets = bk.send(sender, dest, text, host=cfg.host, signed=signed,
                      work_item_id=lot, allow_structured=allow_structured,
                      kind=kind, urgent=urgent, thread_meta=thread_meta,
                      payload=payload or None, cc=cc,
                      include_stopped=queue, skipped=skipped)
    # #1689 (N1) : le marqueur local des destinataires est posé par le dépôt
    # lui-même (`mail.send`, `FileBackend.send`), pour tout émetteur local et
    # pas seulement cette commande.
    if skipped:
        print("warning: not delivered to %s (%s) — --queue to deliver anyway" % (
            ", ".join(sorted(skipped)), "stopped: nobody reads their mailbox"
            if len(skipped) > 1 else "stopped: nobody reads its mailbox"), file=sys.stderr)
    if not targets:
        print("no recipient")
    elif signed:
        print("delivered to %s (signed %s, expires %s)" % (
            ", ".join(targets), signed["signature_key"][:12],
            time.strftime("%H:%M", time.localtime(signed["expires_us"] / 1_000_000))))
    else:
        print("delivered to " + ", ".join(targets) + _send_note(dest, urgent, payload, cc))
    return 0


def _sender_is_human(bk, cfg: Config, sender: str, explicit: bool) -> bool:
    """L125 : l'expéditeur est-il un humain ? Déclaré humain (`AMEESH_HUMANS`),
    ou — nommé par `--from` — humain connu de la base (responsable d'un agent
    ou d'un paquet du canon). Une identité d'agent liée (AGENT_MAIL_NAME,
    liaison de session) n'est jamais humaine. Ne lève jamais."""
    if sender in cfg.human_names:
        return True
    if not explicit or getattr(bk, "kind", None) != "pg":
        return False
    from . import work
    try:
        return bool(work.known_human(bk.db, sender, cfg, canons=lambda: []))
    except Exception:
        return False


def _send_note(dest: str, urgent: bool, payload: dict, cc: list[str]) -> str:
    """L125 : ce que l'envoi réveille, dit à l'expéditeur ('' : message ordinaire)."""
    note = ""
    if payload.get("ack"):
        note = " (acknowledgement: read on its next turn, without waking it)"
    elif dest == "all" and not urgent and not payload.get("human"):
        note = (" (announcement: read on everyone's next turn, no wake-up; "
                "--urgent to wake)")
    if cc:
        note += "; copy without wake-up for: " + ", ".join(cc)
    return note


def cmd_inbox(bk, cfg: Config, rest: list[str]) -> int:
    if rest:
        name = rest[0]  # lire la boîte d'un autre n'accorde rien et ne consomme rien
    else:
        binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
        if not binding.ok:
            print("unbound identity: %s" % (binding.reason or "AGENT_MAIL_NAME not set"),
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
                marque = "  [signed by the owner]"
            elif verdict.ok:
                marque = "  [signed by an agent]"
            else:
                marque = "  [⚠ signature NOT valid: %s]" % verdict.reason
        marque += mail.forwarded_note(msg)
        print("from %s%s: %s" % (msg.get("from"), marque, msg.get("text")))
    return 0


def cmd_list(bk) -> int:
    for row in bk.agents():
        age = int(time.time() - (row.get("last_seen") or 0))
        extra = []
        if row.get("host"):
            extra.append("@" + row["host"])
        if row.get("lease_owner"):
            extra.append("lease:%s" % row["lease_owner"])
        if row.get("status_text"):
            extra.append(row["status_text"])
        print("%-20s %-9s seen %5ss ago  unread:%-3d %s%s" % (
            row["name"], row.get("tool", "?"), age, row.get("unread", 0),
            row.get("cwd") or "", ("  " + " ".join(extra)) if extra else "",
        ))
    return 0


def cmd_status(bk, cfg: Config, rest: list[str]) -> int:
    binding = identity.resolve_binding(cfg, bk.db if bk.kind == "pg" else None)
    if not binding.ok:
        print("unbound identity: %s" % (binding.reason or "AGENT_MAIL_NAME not set"),
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
    print("alias %s → %s%s" % (rest[0], path, (" (workspace %s)" % chantier) if chantier else ""))
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
    # L61 : la même connexion (une seconde coûtait 1 à 2 s vers une base distante)
    bk = _bk
    chantier = identity.chantier_of(name, cfg)

    def lire(db):
        lu = bk if db is None else backend_mod.PgBackend(cfg, db)
        return (lu.get_status(name) if chantier else ""), len(lu.unread(name))

    # statut et non-lus en un seul aller-retour (db.batched)
    status, unread = (db_mod.batched(bk.db, lire) if bk.kind == "pg" else lire(None))
    parts = []
    if chantier:
        parts.append("\033[1;36m[%s/%s]\033[0m%s" % (chantier, name, (" " + status) if status else ""))
    else:
        parts.append("\033[1;36m%s\033[0m" % name)
    parts.append("\033[2m%s\033[0m" % short)
    if unread:
        parts.append("\033[33m✉ %d\033[0m" % unread)
    ligne = "  ·  ".join(parts)
    print(ligne)
    # #1604 : le rendu est fréquent ; la ligne et l'état de la boîte sont
    # réutilisés quelques secondes par le chemin rapide (jamais bloquant).
    try:
        # #1628 : la clé porte le nom résolu, comme celle du chemin rapide.
        fastmail.write_statusline(cfg, fastmail.statusline_key(data, name), ligne)
        if binding.ok:
            fastmail.note(cfg, name, pending=bool(unread), unread=unread)
    except Exception:
        pass
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


class _Remise:
    """Une remise par le hook : le MÊME protocole que l'exécuteur (base).

    Réserver sous un jeton (registre verrouillé, bail de l'identité contrôlé ;
    jamais un message déjà réservé, notamment ceux de la consigne du tour en
    cours), montrer, puis solder cette réservation et elle seule — ou
    l'annuler si rien n'a été montré. Repli fichier : lecture et marquage v0.
    """

    def __init__(self, bk, name: str, binding) -> None:
        self.bk, self.name = bk, name
        self.pg = getattr(bk, "kind", None) == "pg"
        self.owner = binding.runner_id if binding.bound_to_lease else None
        self.epoch = binding.epoch if self.owner else None
        self.jeton = mail.new_token()
        self.msgs: list[dict] = []
        self.ouverte = False
        #: #1604 : la réservation a-t-elle pu laisser des non-lus derrière
        #: elle (plafond atteint) ? Sert au marqueur de boîte, sans requête.
        self.reste = 0
        #: la remise a-t-elle été soldée (remise) ou abandonnée (rendue) ?
        self.solde = False

    def prendre(self, limite: int | None = None) -> list[dict]:
        """Réserve les non-lus ; au plus `limite` (L105 : borne du tour)."""
        if not self.pg:
            tous = self.bk.unread(self.name)
            self.msgs = tous if limite is None else tous[:max(0, limite)]
            self.reste = len(tous) - len(self.msgs)
            return self.msgs
        taille = 200 if limite is None else max(1, limite)
        rows = mail.reserve(self.bk.db, self.name, self.owner, self.epoch, self.jeton,
                            porteur="hook", ttl_seconds=mail.HOOK_RESERVATION_TTL,
                            limit=taille)
        self.ouverte = bool(rows)
        self.msgs = [mail.normalize(row) for row in rows]
        # une réservation pleine peut cacher des non-lus : on le dit (au pire
        # pour rien, le prochain hook reviendra par le chemin complet).
        self.reste = 1 if len(rows) >= taille else 0
        return self.msgs

    def prendre_mi_tour(self, limite: int | None = None) -> list[dict]:
        """L514/#885 : réserve les SEULS messages qui ne peuvent pas attendre.

        Lecture préalable sans verrou (`mail.unread_mid_turn` : le filtre est
        dans la requête, pas dans les `UNREAD_LIMIT` premiers non-lus), puis une
        seule réservation des ids éligibles. Rien d'éligible : aucun geste
        d'écriture — ni verrou de registre, ni libération des autres non-lus
        (le churn d'un `PostToolUse`), et un verdict derrière plus de 200
        non-lus est vu au prochain point sûr, pas à la fin du tour."""
        if not self.pg:
            tous = self.bk.unread(self.name)
            gardes = [m for m in tous if mail.mid_turn(m)]
            self.msgs = gardes if limite is None else gardes[:max(0, limite)]
            self.reste = len(gardes) - len(self.msgs)
            return self.msgs
        if limite is not None and limite <= 0:
            # borne du tour épuisée : rien à montrer, et rien à écrire (même
            # une urgence attend le tour suivant, comme `garder(..., 0)`).
            self.msgs = []
            self.reste = 0
            return []
        taille = mail.UNREAD_LIMIT if limite is None else limite
        voulus = mail.unread_mid_turn(self.bk.db, self.name, taille)
        ids = [int(m["id"]) for m in voulus if m.get("id") is not None]
        if not ids:
            self.msgs = []
            self.reste = 0
            return []
        rows = mail.reserve(self.bk.db, self.name, self.owner, self.epoch, self.jeton,
                            ids=ids, porteur="hook", ttl_seconds=mail.HOOK_RESERVATION_TTL,
                            limit=taille)
        self.ouverte = bool(rows)
        self.msgs = [mail.normalize(row) for row in rows]
        # comme `prendre` : une lecture pleine peut cacher d'autres éligibles
        self.reste = 1 if len(rows) >= taille else 0
        return self.msgs

    def restants_apres(self) -> int:
        """#1604 : non-lus restants après cette remise, sans nouvelle requête.

        Une remise soldée a livré ses messages ; une remise abandonnée les a
        rendus à la boîte. Dans les deux cas, `reste` compte ce que la
        réservation n'a pas pu voir (plafond)."""
        return (0 if self.solde else len(self.msgs)) + self.reste

    def _ids(self) -> list[int]:
        return [int(m["id"]) for m in self.msgs if m.get("id") is not None]

    def solder(self) -> None:
        self.solde = True
        if not self.pg:
            self.bk.mark_read(self.name, self.msgs)
            return
        self.ouverte = False  # montré : jamais annulé, au pire re-livré signalé
        mail.deliver(self.bk.db, self.name, self.owner, self.epoch, self.jeton,
                     self._ids())

    def abandonner(self) -> None:
        if self.pg and self.ouverte:
            self.ouverte = False
            try:
                mail.release(self.bk.db, self.name, self.jeton, self._ids())
            except Exception:  # la réservation expirera d'elle-même
                pass


# --------------------------------------------------------------------------
# courrier borné pendant un tour (L105)
# --------------------------------------------------------------------------

def avis_borne(attente: int, borne: int) -> str:
    """Ce que le hook dit à l'agent quand la borne du tour est atteinte."""
    return ("[ameesh] %d pending message(s) remain for your next turn: at most "
            "%d message(s) are delivered to you during a single turn. Wrap up this turn: "
            "finish the current step, then hand back control; the runner will deliver "
            "the rest on the next turn." % (attente, borne))


def avis_borne_travail(attente: int, borne: int, travail: list[str]) -> str:
    """Borne atteinte, mais un travail lancé pendant le tour tourne encore :
    le tour n'est pas coupé pour le courrier (le conclure maintenant
    laisserait ce travail sans personne pour en lire le résultat)."""
    return ("[ameesh] %d pending message(s) remain for your next turn: at most "
            "%d message(s) are delivered to you during a single turn. Work started during "
            "this turn is still running (%s): do not wrap up before it ends; then wrap up "
            "this turn, the runner will deliver the rest on the next turn."
            % (attente, borne, "; ".join(travail[:3])
               + ("; …" if len(travail) > 3 else "")))


#: le travail du tour (processus de fond, conteneurs) est relevé au plus à ce
#: rythme par le hook, une fois la borne du courrier atteinte (secondes)
TRAVAIL_RELEVE_S = 30.0


def _travail_du_tour(cfg: Config, bk, tour: str, etat: dict) -> list[str]:
    """Le travail lancé pendant ce tour qui tourne encore : tant qu'il y en a,
    la borne du courrier ne demande pas de conclure. Relevé espacé (l'état
    du tour garde le dernier relevé)."""
    maintenant = time.time()
    if maintenant - float(etat.get("travail_ts") or 0.0) < TRAVAIL_RELEVE_S:
        return list(etat.get("travail") or [])
    from . import background
    travail = background.hook_jobs(cfg, bk.db if bk.kind == "pg" else None, tour)
    etat["travail"], etat["travail_ts"] = travail, maintenant
    return travail


def tour_path(cfg: Config, name: str) -> str:
    """État du courrier remis par le hook pendant le tour en cours (L105)."""
    return os.path.join(cfg.state_dir, "hooks", name + ".tour.json")


def tour_lu(cfg: Config, name: str) -> dict:
    """Dernier état du courrier de tour, quel que soit le tour nommé.

    Entre deux tours, le tour courant n'est plus nommé et l'état garde la
    dernière remise : c'est là que L81 lit l'instant du dernier message
    humain remis par le hook (le message n'est alors plus non-lu)."""
    try:
        with open(tour_path(cfg, name), encoding="utf-8") as fh:
            etat = json.load(fh)
        return etat if isinstance(etat, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def tour_lit(cfg: Config, name: str, turn: str) -> dict:
    """Compteur du tour `turn` ; un autre tour (ou rien de lisible) : zéro."""
    etat = tour_lu(cfg, name)
    try:
        if etat.get("turn") == turn:
            return {"turn": turn, "remis": int(etat.get("remis") or 0),
                    "attente": int(etat.get("attente") or 0),
                    "humain": float(etat.get("humain") or 0.0),
                    "signale": bool(etat.get("signale")),
                    # borne atteinte pendant un travail du tour (dit une fois)
                    "differe": bool(etat.get("differe")),
                    "travail": [str(t) for t in etat.get("travail") or []],
                    "travail_ts": float(etat.get("travail_ts") or 0.0)}
    except (ValueError, TypeError):
        pass
    return {"turn": turn, "remis": 0, "attente": 0, "signale": False, "differe": False,
            "travail": [], "travail_ts": 0.0}


def _tour_ecrit(cfg: Config, name: str, etat: dict) -> None:
    path = tour_path(cfg, name)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = "%s.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(etat, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _humain(cfg: Config, row: dict) -> bool:
    """L125/L81 : message d'un humain — marqué à l'envoi, ou expéditeur
    déclaré humain sur cet hôte (`AMEESH_HUMANS`)."""
    return mail.is_human(row) or (row.get("sender") or "") in cfg.human_names


def _tour_remis(cfg: Config, name: str, etat: dict, msgs: list[dict]) -> None:
    """Compteur du tour, et (L81 §6.3) l'instant du dernier message humain
    remis : le message n'est plus non-lu quand l'exécuteur décide de
    l'entretien résident, c'est cet instant qui tient la fenêtre de calme."""
    etat["remis"] = int(etat.get("remis") or 0) + len(msgs)
    if any(_humain(cfg, m) for m in msgs):
        etat["humain"] = time.time()
    _tour_ecrit(cfg, name, etat)


def _attente(bk, name: str, hors: list[dict]) -> int:
    """Non-lus restants, hors des messages de cette remise."""
    ids = {m.get("id") for m in hors if m.get("id") is not None}
    fichiers = {m.get("_file") for m in hors if m.get("_file")}

    def reste(m: dict) -> bool:
        if m.get("id") is not None:
            return m["id"] not in ids
        return m.get("_file") not in fichiers
    try:
        return sum(1 for m in bk.unread(name) if reste(m))
    except Exception:
        return 0


def _marque_boite(cfg: Config, name: str, attente: int) -> None:
    """#1604 : note l'état de la boîte après un passage COMPLET par la base.

    Le chemin rapide du hook ne s'autorise à ne pas ouvrir la base que sur un
    marqueur frais ; ce marqueur ne se pose donc qu'ici, après une lecture
    réelle, et jamais sur une exception. Aucune requête : `attente` vient de la
    réservation (ou du compte déjà lu). Best-effort : un cache qui échoue ne
    fait pas échouer le hook."""
    try:
        fastmail.note(cfg, name, pending=bool(attente), unread=int(attente))
    except Exception:
        pass


def _register_stamp(cfg: Config, name: str) -> str:
    return fastmail.register_stamp(cfg, name)


def _register_due(cfg: Config, name: str, key: list) -> bool:
    """L61 : l'inscription de cette session est-elle à refaire ? Oui si la
    clé (harnais, dossier, session, bail) a changé ou si la dernière date de
    plus de REGISTER_EVERY_S. Un état local illisible : oui. #1604 : le même
    format est relu sans base par le chemin rapide (`fastmail`)."""
    return fastmail.register_due(cfg, name, key)


def _register_done(cfg: Config, name: str, key: list) -> None:
    fastmail.register_done(cfg, name, key)


def hook_foreign_harness(tool: str) -> str:
    """L224 : le harnais du tour (`AMEESH_HARNESS`, posé par l'exécuteur)
    quand ce hook vient d'un AUTRE harnais ; "" sinon.

    Un processus lancé pendant le tour (`codex exec` de relecture sous un
    tour claude) hérite de l'environnement du tour : son hook se présentait
    sous l'identité de l'agent, avec sa propre session (incident mesh-design
    du 2026-10-11)."""
    attendu = os.environ.get("AMEESH_HARNESS") or ""
    return attendu if attendu and tool != attendu else ""


def _foreign_hook_refused(cfg: Config, bk, name: str, tool: str, attendu: str,
                          session_id) -> None:
    """L224 : refus dit sur stderr à chaque fois, journalisé au fil une seule
    fois par (harnais, session) — un PostToolUse par appel d'outil sinon."""
    session = str(session_id or "")
    print("agent-mail: hook %s refused for %s (harness of the turn: %s; session %s) — "
          "neither registration nor mail" % (tool, name, attendu, session or "?"),
          file=sys.stderr)
    if bk.kind != "pg":
        return
    import hashlib
    marque = os.path.join(cfg.state_dir, "hooks", "%s.refus-%s" % (
        name, hashlib.sha256(("%s\0%s" % (tool, session)).encode()).hexdigest()[:16]))
    if os.path.exists(marque):
        return
    try:
        os.makedirs(os.path.dirname(marque), mode=0o700, exist_ok=True)
        with open(marque, "w", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (tool, session))
    except OSError:
        pass
    try:
        row = storage.of(bk.db).agents.get(name)
    except db_mod.DbError:
        row = None
    fil.record(cfg, bk.db, sender="ameesh", recipients=[name],
               text="Hook %s refused for %s: the turn runs under %s. Session %s "
                    "is not the agent's (process launched during the turn with "
                    "its environment): neither registered nor served mail."
                    % (tool, name, attendu, session or "?"),
               project=fil.project_for(cfg, fil.agent_project(row)),
               meta={"action": "hook-refuse", "harness": tool, "attendu": attendu,
                     "session": session})


def hook_subagent(data: dict) -> str:
    """L133 : identifiant du sous-agent où ce hook se déclenche ; "" pour la
    session principale.

    Claude Code (sous-agent lancé par l'outil Agent/Task, coéquipier en
    processus) et Codex (sous-agent lancé par spawn_agent) déclenchent les
    hooks ordinaires (PostToolUse…) dans leurs sous-agents avec le
    `session_id` de la session principale, et y ajoutent `agent_id`, absent
    sur la session principale. `agent_type` seul ne désigne pas un
    sous-agent : Claude Code le pose aussi sur une session principale lancée
    avec `--agent`."""
    agent_id = data.get("agent_id")
    if agent_id is None:
        return ""
    return str(agent_id).strip()


#: L514: harnesses whose turn gets, between two tool calls, only the mail
#: `mail.mid_turn` accepts; Claude keeps getting all of it
MID_TURN_HARNESSES = ("deepseek", "codex")


def mid_turn_filtered(tool: str, binding) -> bool:
    """L514: a PostToolUse of this harness, in a turn the runner drives,
    delivers only verdicts, arbitrations, human and urgent mail; the rest
    keeps the L125 batching until the end of the turn."""
    return tool in MID_TURN_HARNESSES and bool(binding.bound_to_lease)


def cmd_hook(cfg: Config, tool: str) -> int:
    """Hook de harnais : lit le JSON sur stdin, ne fait JAMAIS échouer l'agent."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    if hook_subagent(data):
        # L133 : le courrier de l'agent n'est jamais remis à l'un de ses
        # sous-agents, qui l'ignorerait alors qu'il serait marqué livré. Rien
        # n'est lu, remis, marqué ni écrit (inscription, titre, compteurs) :
        # le courrier attend le prochain hook de la session principale.
        return 0
    event = data.get("hook_event_name") or ""
    try:
        bk, _warning = backend_mod.open_backend(cfg)
    except Exception:
        bk = backend_mod.FileBackend(cfg)
    # L'identité vient de l'environnement (le runner la pose), jamais du dossier :
    # lire la doc d'un autre agent dans son worktree ne consomme plus son courrier.
    # L41 (0030) : sans AGENT_MAIL_NAME, seule la liaison explicite de CETTE
    # session (hôte, harnais, identifiant reçu en JSON ; PID ancêtre s'il est
    # lié) donne une identité. Sans liaison : rien n'est remis, rien n'est écrit.
    try:
        binding = identity.resolve_binding(
            cfg, bk.db if bk.kind == "pg" else None,
            harness=tool, session_id=str(data.get("session_id") or ""))
    except db_mod.DbError:
        # L61 : la vérification de schéma voyage avec la première requête ;
        # un schéma absent (ou une base tombée entre-temps) donne le même
        # repli fichier qu'avant, quand open_backend le détectait.
        bk.close()
        bk = backend_mod.FileBackend(cfg)
        binding = identity.resolve_binding(
            cfg, None, harness=tool, session_id=str(data.get("session_id") or ""))
    if not binding.ok:
        if binding.name:
            # Identité annoncée mais bail invalide : on le dit sur stderr, et
            # on ne touche à rien.
            print("agent-mail: identity %s unbound (%s) — mail left in place"
                  % (binding.name, binding.reason), file=sys.stderr)
        return 0
    name = binding.name
    if not getattr(bk, "hook_delivery", True):
        # Mediated session hooks leave delivery to the executor's next turn.
        return 0
    attendu = hook_foreign_harness(tool)
    if attendu:
        # Only the agent's own harness may register sessions or deliver mail.
        _foreign_hook_refused(cfg, bk, name, tool, attendu, data.get("session_id"))
        return 0
    if binding.source == "session" and binding.ok:
        # #1604 : l'identité de CETTE session est notée localement pour que les
        # hooks rapides (PostToolUse) n'aient pas à rouvrir la base ; le
        # marqueur de boîte, lui, reste seul juge de ce qu'il y a à faire.
        fastmail.remember_liaison(cfg, name, tool, str(data.get("session_id") or ""),
                                  pid=(getattr(binding, "session", None) or {}).get("pid"))
    # L166 : sous bail, la session de l'agent est celle que l'exécuteur lit
    # dans le flux du harnais, jamais celle d'un hook. dsh déclenche les hooks
    # de ses agents enfants (sous-agent, fourche) avec LEUR session et sans
    # `agent_id` : inscrite ici, elle devenait la session reprise au tour
    # suivant, que dsh refuse (« is a subagent or forked session »).
    if binding.source == "explicit" and bk.kind == "pg":
        # L46 : AGENT_MAIL_NAME sans bail (pont local, shell qui en a hérité)
        # ne prend jamais le courrier d'un agent mené par l'exécuteur (bail
        # vivant). La réservation le refuse aussi, sous verrou ; ce contrôle-ci
        # le dit. #1689 (N2) : le refus précède l'inscription — sinon le
        # tampon d'inscription de ce shell (clé sans bail) faisait dire au
        # chemin rapide « inscription à jour », et le marqueur local taisait ce
        # diagnostic à chaque PostToolUse.
        try:
            holder = sb.lease_holder(bk.db, name)
        except Exception:
            return 0
        if holder:
            print("agent-mail: %s is driven by the runner (live lease of %s) — "
                  "mail left in place" % (name, holder), file=sys.stderr)
            return 0
    session_id = None if binding.bound_to_lease else data.get("session_id")
    try:
        inscription = [tool, data.get("cwd"), session_id,
                       binding.runner_id if binding.bound_to_lease else None,
                       binding.epoch if binding.bound_to_lease else None]
        if bk.kind != "pg" or _register_due(cfg, name, inscription):
            bk.register(name, tool, data.get("cwd"), session_id,
                        leased=binding.bound_to_lease)
            if bk.kind == "pg":
                # L52: record only after L133/L166 have filtered child IDs.
                # Under a runner lease, session_id is None; stream parsing
                # and registry.set_session own the authoritative session.
                if session_id:
                    registry.record_session_history(bk.db, name, str(session_id))
                _register_done(cfg, name, inscription)
        set_title(cfg, bk, name)
        if event not in ("Stop", "SessionStart", "UserPromptSubmit", "PostToolUse"):
            return 0
        if event == "UserPromptSubmit":
            bk.stop_counter(name, reset=True)
        # L105 : pendant un tour mené par l'exécuteur, au plus `turn_mail_max`
        # messages remis ; au-delà, rien n'est réservé (ils restent pour le
        # tour suivant), le Stop n'est plus bloqué, et l'agent est invité à
        # conclure son tour — jamais tant qu'un travail lancé pendant le tour
        # tourne encore (processus de fond, conteneurs du tour) : il le saura,
        # et l'invitation viendra après.
        # L76 : un processus résident garde son environnement d'un tour à
        # l'autre — le tour en cours est nommé dans un fichier
        from . import resident_runner
        tour = resident_runner.current_turn()
        borne = cfg.turn_mail_max if (tour and binding.bound_to_lease) else 0
        etat = tour_lit(cfg, name, tour) if (tour and binding.bound_to_lease) else None
        if etat is not None and borne and etat["remis"] >= borne:
            if event == "Stop":
                return 0
            attente = _attente(bk, name, [])
            etat["attente"] = attente
            if attente and not etat["signale"]:
                travail = _travail_du_tour(cfg, bk, tour, etat)
                if not travail:
                    if _emit({"hookSpecificOutput": {
                            "hookEventName": event,
                            "additionalContext": avis_borne(attente, borne)}}):
                        etat["signale"] = True
                elif not etat["differe"] and _emit({"hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": avis_borne_travail(attente, borne, travail)}}):
                    etat["differe"] = True
            _tour_ecrit(cfg, name, etat)
            # La borne n'a rien réservé : le marqueur dit le non-lu restant,
            # pour que le chemin rapide ne conclue pas « rien à faire ».
            _marque_boite(cfg, name, attente)
            return 0
        remise = _Remise(bk, name, binding)
        #: #1628 : un drapeau local, pas `sys.exc_info()` — un `cmd_hook` appelé
        #: depuis un `except` verrait l'exception extérieure et ne marquerait
        #: jamais la boîte.
        remise_ok = True
        try:
            # #289 : la garde `and borne` date la remise a zero (une borne
            # nulle n'a rien reserve) ; la structure L514 de la chaine est
            # gardee (limite, garder/prendre selon le filtre mi-tour).
            limite = borne - etat["remis"] if etat is not None and borne else None
            if event == "PostToolUse" and mid_turn_filtered(tool, binding):
                # L514/#885: in a dsh or codex turn, only what must not wait
                msgs = remise.prendre_mi_tour(limite)
            else:
                msgs = remise.prendre(limite)
            avis = ""
            if etat is not None and borne and msgs and etat["remis"] + len(msgs) >= borne:
                attente = _attente(bk, name, msgs)
                etat["attente"] = attente
                if attente:
                    travail = _travail_du_tour(cfg, bk, tour, etat)
                    if travail:
                        avis = "\n\n" + avis_borne_travail(attente, borne, travail)
                        etat["differe"] = True
                    else:
                        avis = "\n\n" + avis_borne(attente, borne)
                        etat["signale"] = True
            if event == "Stop":
                # L125 : un courrier passif (accusé, copie, diffusion) ne relance
                # pas un tour qui se termine : rendu à la boîte, il attend le
                # tour suivant (la réservation est annulée ci-dessous)
                if not msgs or all(mail.passive_reason(m) for m in msgs):
                    bk.stop_counter(name, reset=True)
                    return 0
                # stop_hook_active n'est pas fiable selon les harnais : on compte
                # nous-mêmes les tours forcés d'affilée (comme la v0).
                if bk.stop_counter(name) >= MAX_STOP_BLOCKS:
                    return 0
                # Écrire d'abord : si le harnais a fermé son entrée, on ne
                # consomme pas les messages pour autant.
                if not _emit({"decision": "block",
                              "reason": render(msgs, verdicts_for(bk, msgs)) + avis}):
                    return 0
                remise.solder()
                if etat is not None:
                    _tour_remis(cfg, name, etat, msgs)
                bk.stop_counter(name, bump=True)
                return 0
            if not msgs:
                return 0
            if not _emit({
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": render(msgs, verdicts_for(bk, msgs)) + avis,
                }
            }):
                return 0
            remise.solder()
            if etat is not None:
                _tour_remis(cfg, name, etat, msgs)
        except BaseException:
            # BaseException : une interruption (Ctrl-C, SystemExit) en cours de
            # remise ne doit pas non plus poser le marqueur.
            remise_ok = False
            raise
        finally:
            remise.abandonner()
            # #1604 : marqueur posé seulement après un passage complet sans
            # exception (une base en panne ne doit pas se dire « rien à faire »),
            # et sans requête : la réservation sait ce qu'elle a laissé.
            if remise_ok:
                _marque_boite(cfg, name, remise.restants_apres())
    except Exception:
        return 0
    return 0


# --------------------------------------------------------------------------
# liaisons de session (L41, décision 0030)
# --------------------------------------------------------------------------

def _options(rest: list[str], valued: tuple[str, ...], flags: tuple[str, ...] = (),
             ) -> tuple[dict, list[str]]:
    """Analyse minimale `--opt VALEUR` / `--drapeau` ; lève ValueError si incomplet."""
    opts: dict = {}
    positional: list[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in valued:
            if index + 1 >= len(rest):
                raise ValueError("%s expects a value" % arg)
            opts[arg] = rest[index + 1]
            index += 2
            continue
        if arg in flags:
            opts[arg] = True
        elif arg.startswith("--"):
            raise ValueError("unknown option: %s" % arg)
        else:
            positional.append(arg)
        index += 1
    return opts, positional


def _actor() -> str:
    """Qui lie ou délie : l'agent nommé par l'environnement, sinon l'humain local."""
    name = os.environ.get("AGENT_MAIL_NAME") or ""
    if name and NAME_RE.match(name):
        return name
    import getpass
    return "human:%s" % (os.environ.get("USER") or getpass.getuser())


_BIND_USAGE = ("usage: agent-mail bind <agent> --session ID --harness claude|codex|deepseek "
               "[--pid N] [--force]\n       agent-mail bind --import FILE")


def cmd_bind(bk, cfg: Config, rest: list[str]) -> int:
    """L41 (0030) : lie une session externe à un agent, ou importe le pont local."""
    try:
        opts, positional = _options(rest, ("--session", "--harness", "--pid", "--import"),
                                    ("--force",))
    except ValueError as exc:
        print("%s\n%s" % (exc, _BIND_USAGE), file=sys.stderr)
        return 2
    by = _actor()
    if "--import" in opts:
        if positional or any(k in opts for k in ("--session", "--harness", "--pid",
                                                  "--force")):
            print("--import must be used alone\n" + _BIND_USAGE, file=sys.stderr)
            return 2
        try:
            report = sb.import_file(cfg, bk.db, opts["--import"], by=by)
        except (OSError, ValueError) as exc:
            print("import failed: %s" % exc, file=sys.stderr)
            return 1
        print("bindings imported: %d; skipped: %d" % (
            len(report.imported), len(report.skipped)))
        for session_id, agent, detail in report.imported:
            print("  imported: %s → %s (%s)" % (session_id, agent, detail))
        for session_id, reason in report.skipped:
            print("  skipped : %s — %s" % (session_id, reason))
        return 0
    if len(positional) != 1 or "--session" not in opts or "--harness" not in opts:
        print(_BIND_USAGE, file=sys.stderr)
        return 2
    try:
        result = sb.bind(cfg, bk.db, positional[0], session_id=opts["--session"],
                         harness=opts["--harness"], pid=opts.get("--pid"), by=by,
                         force=bool(opts.get("--force")))
    except sb.BindError as exc:
        print("binding refused: %s" % exc, file=sys.stderr)
        return 1
    row = result.row
    verbe = {"created": "bound", "updated": "re-bound (pid updated)",
             "unchanged": "already bound"}[result.status]
    print("session %s %s %s to %s on %s (pid %s)" % (
        row.get("harness"), row.get("session_id"), verbe, row.get("agent"),
        row.get("host"), row.get("pid") or "not checked"))
    for warning in result.warnings:
        print("warning: %s" % warning, file=sys.stderr)
    return 0


def cmd_unbind(bk, cfg: Config, rest: list[str]) -> int:
    usage = "usage: agent-mail unbind --session ID --harness claude|codex|deepseek"
    try:
        opts, positional = _options(rest, ("--session", "--harness"))
    except ValueError as exc:
        print("%s\n%s" % (exc, usage), file=sys.stderr)
        return 2
    if positional or "--session" not in opts or "--harness" not in opts:
        print(usage, file=sys.stderr)
        return 2
    try:
        row = sb.unbind(cfg, bk.db, session_id=opts["--session"],
                        harness=opts["--harness"], by=_actor())
    except sb.BindError as exc:
        print("%s" % exc, file=sys.stderr)
        return 2
    if row is None:
        print("no active binding for session %s %s on %s"
              % (opts["--harness"], opts["--session"], cfg.host), file=sys.stderr)
        return 1
    print("binding revoked: session %s %s (was %s)" % (
        row.get("harness"), row.get("session_id"), row.get("agent")))
    return 0


def cmd_bindings(bk, rest: list[str]) -> int:
    usage = "usage: agent-mail bindings [--all] [--json]"
    try:
        opts, positional = _options(rest, (), ("--json", "--all"))
    except ValueError as exc:
        print("%s\n%s" % (exc, usage), file=sys.stderr)
        return 2
    if positional:
        print(usage, file=sys.stderr)
        return 2
    rows = sb.listing(bk.db, include_revoked=bool(opts.get("--all")))
    if opts.get("--json"):
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("no session binding")
        return 0
    print("%-20s %-8s %-38s %-8s %-12s %-16s %s" % (
        "AGENT", "HARNESS", "SESSION", "PID", "HOST", "BOUND AT", "BY"))
    for row in rows:
        quand = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("created_ts") or 0))
        etat = ""
        if row.get("revoked_ts"):
            etat = "  (revoked %s)" % time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(row["revoked_ts"]))
        print("%-20s %-8s %-38s %-8s %-12s %-16s %s%s" % (
            row.get("agent"), row.get("harness"), row.get("session_id"),
            row.get("pid") or "-", row.get("host"), quand, row.get("created_by"), etat))
    return 0


_FORWARD_USAGE = "usage: agent-mail forward <old> <new> [--dry-run] [--json]"


def cmd_forward(bk, cfg: Config, rest: list[str]) -> int:
    """Vide une boîte morte : re-livre le courrier en attente d'un agent arrêté
    (ou absent du registre) à un agent vivant (`undeliverable.forward`)."""
    from . import undeliverable
    try:
        opts, positional = _options(rest, (), ("--dry-run", "--json"))
    except ValueError as exc:
        print("%s\n%s" % (exc, _FORWARD_USAGE), file=sys.stderr)
        return 2
    if len(positional) != 2 or not all(NAME_RE.match(name) for name in positional):
        print(_FORWARD_USAGE, file=sys.stderr)
        return 2
    old, new = positional
    try:
        result = undeliverable.forward(cfg, bk.db, old, new, actor=_actor(),
                                       dry_run=bool(opts.get("--dry-run")))
    except undeliverable.Refused as exc:
        print("forward refused — %s" % exc, file=sys.stderr)
        return 1
    if opts.get("--json"):
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if result["dry_run"]:
        rows = result["messages"]
        if not rows:
            print("no pending message for %s: nothing to forward" % old)
            return 0
        print("dry run: %d pending message(s) for %s (%s) would be re-delivered to %s:"
              % (len(rows), old, undeliverable.state_label(result["old_state"]), new))
        for row in rows:
            print("  #%s from %s, %s%s" % (
                row["id"], row.get("sender") or "?",
                time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("created_ts") or 0)),
                " (%s)" % row["kind"] if row.get("kind") not in (None, "notify") else ""))
        return 0
    moved = result["forwarded"]
    if not moved:
        print("no pending message for %s: nothing to forward" % old)
        return 0
    # #1604 : le successeur local a du courrier neuf ; son marqueur le dit.
    fastmail.bump(cfg, new)
    senders: dict = {}
    for row in moved:
        senders[row.get("sender") or "?"] = senders.get(row.get("sender") or "?", 0) + 1
    print("%d message(s) re-delivered from %s (%s) to %s: %s" % (
        len(moved), old, undeliverable.state_label(result["old_state"]), new,
        ", ".join("#%s → #%s" % (r["original_id"], r["new_id"]) for r in moved)))
    print("senders: %s; original sender and date kept, forward noted in the thread"
          % ", ".join("%s (%d)" % kv for kv in sorted(senders.items(),
                                                       key=lambda kv: (-kv[1], kv[0]))))
    return 0


_SWEEP_USAGE = "usage: agent-mail sweep [--days N] [--dry-run] [--json]"


def _sweep_line(action, kind) -> str:
    """Une ligne du compte rendu de `ameesh mail sweep`."""
    ids = action.get("messages") or []
    suffix = " (#%s)" % ", #".join(str(i) for i in ids) if ids else ""
    if kind == "forwarded":
        return ("%s (stopped): %d message(s) %s re-delivered to %s%s"
                % (action["old"], action["count"],
                   "would be" if action.get("dry_run") else "were", action["new"], suffix))
    return ("%s (%s): %d message(s) %s expired%s"
            % (action["recipient"], "unknown" if action.get("unknown") else "stopped",
               action["count"], "would be" if action.get("dry_run") else "were", suffix))


def cmd_sweep(bk, cfg: Config, rest: list[str]) -> int:
    """L976 : re-livre au successeur, ou expire après la rétention, le courrier
    en souffrance (`undeliverable.sweep`) — le même passage que le ménage de
    l'exécuteur, à la main."""
    from . import undeliverable
    try:
        opts, positional = _options(rest, ("--days",), ("--dry-run", "--json"))
    except ValueError as exc:
        print("%s\n%s" % (exc, _SWEEP_USAGE), file=sys.stderr)
        return 2
    if positional:
        print(_SWEEP_USAGE, file=sys.stderr)
        return 2
    days = opts.get("--days")
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            print("--days expects a whole number of days\n%s" % _SWEEP_USAGE, file=sys.stderr)
            return 2
        if days < 0:
            print("--days must be >= 0\n%s" % _SWEEP_USAGE, file=sys.stderr)
            return 2
    try:
        result = undeliverable.sweep(cfg, bk.db, days=days, actor=_actor(),
                                     dry_run=bool(opts.get("--dry-run")))
    except ValueError as exc:
        print("sweep refused — %s\n%s" % (exc, _SWEEP_USAGE), file=sys.stderr)
        return 1
    if opts.get("--json"):
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if result["refused"] else 0
    actions = result["forwarded"] + result["expired"]
    if not actions and not result["refused"]:
        print("no dead letter to sweep (retention %d day(s), 0 = never)"
              % result["days"])
        return 0
    if not actions:
        # #1396/N2 : que des refus de renvoi — le dire (le courrier n'est pas
        # remis) au lieu d'annoncer « no dead letter », et sortir non-zéro.
        print("no dead letter swept: %d re-delivery refusal(s) (retention %d day(s), "
              "0 = never)" % (len(result["refused"]), result["days"]))
    elif result["dry_run"]:
        print("dry run: retention %d day(s); %d recipient(s) to sweep:"
              % (result["days"], len(actions)))
    for action in result["forwarded"]:
        print("  " + _sweep_line(action, "forwarded"))
    for action in result["expired"]:
        print("  " + _sweep_line(action, "expired"))
    for action in result["refused"]:
        print("  %s: re-delivery to %s refused — %s" % (
            action["old"], action["new"], action["reason"].splitlines()[0]), file=sys.stderr)
    if not result["dry_run"] and actions:
        # #1604 : les successeurs locaux ont du courrier neuf.
        for action in result["forwarded"]:
            fastmail.bump(cfg, action.get("new") or "")
        print("retention %d day(s); dead mail stays in the mailbox, marked expired"
              % result["days"])
    return 1 if result["refused"] else 0


def cmd_whoami(bk, cfg: Config, rest: list[str]) -> int:
    if len(rest) > 1 and rest[0] == "--cwd":
        # Diagnostic : le dossier ne donne aucune identité.
        print("legacy %s (diagnostic, not authoritative)"
              % identity.legacy_name(rest[1], cfg))
        return 0
    try:
        opts, _positional = _options(rest, ("--session", "--harness"))
    except ValueError as exc:
        print("%s\nusage: agent-mail whoami [--session ID --harness H] | --cwd DIR"
              % exc, file=sys.stderr)
        return 2
    db = bk.db if bk.kind == "pg" else None
    if "--session" in opts or "--harness" in opts:
        # L41 (0030) : la liaison d'une session nommée, comme le hook la verrait
        # (le PID lié doit être un ancêtre de cette commande).
        binding = identity.resolve_binding(cfg, db, harness=opts.get("--harness") or "",
                                           session_id=opts.get("--session") or "")
    else:
        binding = identity.resolve_binding(cfg, db)
    if not binding.ok:
        print("unbound identity: %s" % (binding.reason or "AGENT_MAIL_NAME not set"),
              file=sys.stderr)
        if binding.session:
            print("(%s)" % binding.describe_session(), file=sys.stderr)
        return 1
    if binding.source == "explicit":
        print(binding.name)
    elif binding.source == "session":
        # L41 (0030) : la source et la liaison, pour qu'une session sache
        # pourquoi elle reçoit ce courrier
        print("%s [session] %s" % (binding.name, binding.describe_session()))
    else:
        print("%s [%s]" % (binding.name, binding.source))
    return 0


def cmd_migrate(cfg: Config, dry_run: bool = False, additive_only: bool = False) -> int:
    """`--dry-run` : migrations manquantes, additives ou non, sans rien écrire.
    `--additive-only` : n'applique rien (code 3) si l'une d'elles n'est pas
    additive — c'est le garde de « migrer avant d'installer » (L151)."""
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("migration failed: %s" % exc, file=sys.stderr)
        return 1
    try:
        if dry_run or additive_only:
            missing, modified, _unknown = migrations.check(db)
            if modified:
                print("migration failed: %s modified after being applied"
                      % ",".join(m.label for m in modified), file=sys.stderr)
                return 1
            refused = []
            for migration in missing:
                reasons = migrations.non_additive(migration)
                if reasons:
                    refused.append(migration)
                if dry_run or reasons:
                    print("%s: %s" % (migration.label, "NOT additive — " + "; ".join(reasons)
                                       if reasons else "additive"))
            if refused:
                print("%d non-additive migration(s): the previous code would break on the "
                      "migrated database" % len(refused), file=sys.stderr)
            if dry_run:
                if not missing:
                    print("schema up to date: no migration to apply")
                return 0
            if refused:
                print("nothing applied (--additive-only)", file=sys.stderr)
                return 3
        done = migrations.migrate(db, log=lambda line: print(line))
    except db_mod.DbError as exc:
        print("migration failed: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
    if not done:
        print("schema up to date: no migration to apply")
    else:
        print("%d migration(s) applied" % len(done))
    return 0


def cmd_probe(cfg: Config) -> int:
    """Sonde légère (`doctor --probe`) : base joignable, schéma présent,
    migrations à jour. Ni comptage, ni NOTIFY, ni écriture : faite pour une
    sonde de conteneur appelée toutes les quelques secondes. Une ligne, code 0
    (OK) ou 1 (KO)."""
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("probe: KO — database unreachable (%s)" % exc)
        return 1
    try:
        db_mod.require_schema(db)
        missing, modified, unknown = migrations.check(db)
    except db_mod.SchemaMissing:
        print("probe: KO — schema missing (%s, schema %s)"
              % (config_mod.mask_dsn(cfg.dsn), cfg.schema))
        return 1
    except db_mod.DbError as exc:
        print("probe: KO — %s" % exc)
        return 1
    finally:
        db.close()
    if missing or modified:
        print("probe: KO — migrations %s%s" % (
            "missing %s" % ",".join(m.label for m in missing) if missing else "",
            ("%smodified %s" % ("; " if missing else "", ",".join(m.label for m in modified))
             if modified else "")))
        return 1
    extra = ("; database newer than this code (versions %s)"
             % ",".join("%04d" % v for v in unknown)) if unknown else ""
    print("probe: OK (driver %s, schema %s)%s" % (db.name, cfg.schema, extra))
    return 0


def driver_warnings(driver: str, remote: bool) -> list[str]:
    """L61 : le pilote psql ouvre une connexion (TLS compris) par requête ;
    sur une base distante, c'est 1 à 2 s par requête. psycopg est la voie
    normale (dépendance du paquet) : son absence se corrige ici."""
    if driver != "psql" or not remote:
        return []
    return [
        "warning    : psql driver with a REMOTE database — one subprocess and one "
        "TLS connection per query (~1.5 s each at 180 ms round trip);",
        "             install psycopg in ameesh's environment: %s -m pip install "
        "'psycopg[binary]>=3.1'" % sys.executable,
    ]


def host_measures_line(cfg: Config, reading: dict | None = None) -> str:
    """L64: the host measures this OS, or this host, cannot give, read on a
    fresh reading (never the published one, which may come from before an
    upgrade) of the filesystems the guard watches (work roots, state, caches,
    tmp, docker, `/`: a btrfs work root is not the state dir's filesystem),
    under the guard's effective housekeeping policy (the canons' Host card:
    a cache root of its own); the default one, said, when the canons cannot
    be read. Informative: the verdict does not change."""
    from . import canon as canon_mod
    from . import menage as menage_mod
    from . import resources
    note = ""
    try:
        if reading is None:
            try:
                policy = menage_mod.effective(canon_mod.load_configured(cfg), cfg.host or "")
            except Exception:
                policy, note = menage_mod.Policy(), " [default policy: canon unreadable]"
            reading = resources.sample(cfg.host, paths=resources.disk_paths(cfg, policy),
                                       tmp=resources.tmp_paths(cfg, policy))
        missing = resources.unmeasured(reading)
    except Exception as exc:  # a reading never breaks the doctor
        return "measures   : host not read (%s)" % (" ".join(str(exc).split())[:120],)
    if not missing:
        return "measures   : every host measure is read (%s)%s" % (platform.SYSTEM, note)
    return "measures   : not given on this host (%s): %s%s" % (platform.SYSTEM,
                                                               ", ".join(missing), note)


def cmd_doctor(cfg: Config, notify_test: bool, probe: bool = False,
               harness: bool = False) -> int:
    if probe:
        return cmd_probe(cfg)
    if harness:
        # L106 : binaires des harnais et unités d'exécuteur de l'hôte
        from . import hostcheck
        try:
            db = db_mod.connect(cfg)
        except db_mod.Unavailable as exc:
            print("database   : unreachable (%s) — harnesses checked without the agents" % exc)
            return hostcheck.cmd_harness(cfg)
        try:
            return hostcheck.cmd_harness(cfg, db)
        finally:
            db.close()
    from . import version
    print("version    : ameesh %s" % version())
    print("dsn        : %s" % config_mod.mask_dsn(cfg.dsn))
    print("schema     : %s" % cfg.schema)
    print("host       : %s" % cfg.host)
    from . import persona_memory
    from . import canon as canon_mod
    memory_note = ""
    try:
        policies = [(canon.id, persona_memory.isolation_policy(cfg, canon.registry_canon))
                    for canon in canon_mod.load_configured(cfg)]
    except Exception:  # an unreadable policy never breaks the diagnostic
        policies = []
        memory_note = " [approved canon unreadable; exception not verified]"
    exceptions = [ident for ident, policy in policies if policy == "shared-uid-accepted"]
    print("memory     : %s%s" % ("shared-uid-accepted (approved canon: %s; no UID isolation)"
                                 % ", ".join(exceptions) if exceptions else
                                 "UID isolation required; active host peers disable transport",
                                 memory_note))
    started = time.monotonic()
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("driver     : none (%s)" % exc)
        print("verdict    : KO — the CLI would fall back to the files %s" % cfg.v0_state)
        return 1
    connected = time.monotonic() - started
    print("driver     : %s" % db.name)
    remote = db_mod.is_remote(cfg)
    if remote:
        # L61 : sur une base distante, chaque aller-retour coûte la latence
        # du réseau ; la connexion (TCP + TLS + authentification) plusieurs.
        started = time.monotonic()
        try:
            db.ping()
            trip = time.monotonic() - started
            print("network    : remote database; connection %d ms, simple query %d ms"
                  % (connected * 1000, trip * 1000))
        except db_mod.DbError:
            pass
    for line in driver_warnings(db.name, remote):
        print(line)
    if remote and db.name == "psycopg" and "sslnegotiation" not in (cfg.dsn or ""):
        # L61 : négociation TLS directe (libpq et serveur ≥ 17) — un
        # aller-retour de moins à chaque connexion (1,31 → 1,12 s mesuré)
        try:
            import psycopg
            info = db.conn.info
            if (psycopg.pq.version() >= 170000 and info.server_version >= 170000
                    and db.conn.pgconn.ssl_in_use):
                print("hint       : add sslnegotiation=direct to the DSN (libpq and server "
                      "≥ 17): one round trip less per connection")
        except Exception:
            pass
    try:
        db_mod.require_schema(db)
    except db_mod.SchemaMissing as exc:
        print("schema     : missing (%s)" % exc)
        print("verdict    : KO — run \"agent-mesh migrate\"")
        return 1
    known, done = migrations.status(db)
    print("migrations : %d/%d applied" % (len(done), len(known)))
    agents = storage.of(db).agents.count()
    unread = storage.of(db).mailbox.unread_total()
    print("agents     : %d" % agents)
    print("unread     : %d" % unread)
    print(host_measures_line(cfg))
    # L106 : un agent mené sans unité d'exécuteur ne repart pas après une
    # coupure de l'hôte (signalé, sans changer le verdict)
    from . import hostcheck
    try:
        for line in hostcheck.missing_runner_lines(cfg, db):
            print(line)
        # L159c : plafond mémoire et slice des unités d'agent
        for line in hostcheck.memory_cap_lines(cfg, db):
            print(line)
    except db_mod.DbError:
        pass
    verdict = 0
    from . import db_capacity
    try:
        capacity = db_capacity.read(db)
        if capacity is not None:
            print("connections: %s" % db_capacity.line(capacity))
            if db_capacity.pressured(capacity):
                verdict = 1
    except db_mod.DbError:
        print("connections: unavailable")
        verdict = 1
    if notify_test:
        channel = "ameesh_doctor"
        listener = storage.of(db).wakeups.subscribe([channel])
        if listener is None:
            print("notify     : driver without LISTEN/NOTIFY")
            verdict = 1
        else:
            time.sleep(0.3)
            storage.of(db).wakeups.notify(channel, "ping")
            got = listener.wait(timeout=3.0)
            listener.close()
            if got and got.get("payload") == "ping":
                print("notify     : OK (LISTEN/NOTIFY end to end)")
            else:
                print("notify     : KO (%r)" % (got,))
                verdict = 1
        # L66 : le canal `desktop` tel que CET OS l'affiche (notify-send,
        # osascript, toast WSL2) ; un envoi reel s'il est route.
        from . import notify as notify_mod
        ok, line = notify_mod.desktop_probe(cfg)
        print("desktop    : %s" % line)
        if not ok:
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
    # `--help` n'exécute JAMAIS une sous-commande (une relecture a appliqué des
    # migrations à une base vivante avec `mail migrate --help`). Pour `send`, le
    # texte peut contenir « --help » : seule la première position compte.
    if (rest[:1] in (["-h"], ["--help"]) if command == "send"
            else any(arg in ("-h", "--help") for arg in rest)):
        print(__doc__)
        return 0
    cfg = config_mod.load()
    try:
        if command == "hook":
            return cmd_hook(cfg, rest[0] if rest else "?")
        if command == "migrate":
            return cmd_migrate(cfg, dry_run="--dry-run" in rest,
                               additive_only="--additive-only" in rest)
        if command == "doctor":
            return cmd_doctor(cfg, notify_test="--notify-test" in rest,
                              probe="--probe" in rest, harness="--harness" in rest)
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
            return cmd_whoami(bk, cfg, rest)
        if command == "forward":
            if bk.kind != "pg":
                print("forwarding requires the database (AMEESH_DSN): the file fallback has no "
                      "registry", file=sys.stderr)
                return 1
            return cmd_forward(bk, cfg, rest)
        if command == "sweep":
            if bk.kind != "pg":
                print("the dead-letter sweep requires the database (AMEESH_DSN): the file "
                      "fallback has no registry", file=sys.stderr)
                return 1
            return cmd_sweep(bk, cfg, rest)
        if command in ("bind", "unbind", "bindings"):
            if bk.kind != "pg":
                print("session bindings require the database (AMEESH_DSN): "
                      "the file fallback has none", file=sys.stderr)
                return 1
            if command == "bind":
                return cmd_bind(bk, cfg, rest)
            if command == "unbind":
                return cmd_unbind(bk, cfg, rest)
            return cmd_bindings(bk, rest)
        if command == "status":
            return cmd_status(bk, cfg, rest)
        print(__doc__)
        return 2
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
