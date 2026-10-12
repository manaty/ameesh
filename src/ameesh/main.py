#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh — the mesh command, and its subcommands.

  ameesh --version | version  version of the installed package;
  ameesh mail <send|inbox|list|status|alias|hook|statusline|whoami>
        the mailbox; `agent-mail` remains an alias (harness hooks call it);
        `send` refuses an unknown recipient (close names suggested) or a
        stopped one (unless --queue);
  ameesh mail forward <old> <new> [--dry-run]
        re-delivers the pending mail of a stopped agent to a live agent;
  ameesh mail <bind|unbind|bindings>
        explicit binding of an external session to an agent (L41, 0030);
  ameesh run [options]        the machine's runner; `agent-runner` remains
        an alias;
  ameesh activity <persona> [--since 24h] [--json]   persona activity log (L59);
  ameesh attach <agent> [--wait] [--ttl S]
        an interactive session on the agent's lease, same session (C9);
  ameesh list | show          observability (host, lease, unread, budget, key);
  ameesh key …                public keys (owner / agent);
  ameesh approve | approvals | verify   signed approvals;
  ameesh work …               work items; `work backlog add|list`: continuous
        improvement queue, picked up by idle agents (L119, 0037);
        `work unrecorded [--since 24h] [--json]`: human requests (mail,
        decision answers, chat) not attached to any work item (L130);
  ameesh cost …               turn cost, per-turn usage, gauges and their
        history, pay-per-token provider balance (`cost turns|gauges|balance`);
  ameesh accounts list | use <harness> <account> | auto [harness]
        multiple accounts per provider: active, gauges, override (L30);
  ameesh budget [--json] | set --per-hour X [--per-day Y] [--agent A]
        | unset [--per-hour] [--per-day] [--agent A]
        mesh budget caps, stored in the database, reloaded live (L70);
  ameesh heavy [--nice N] -- <command> | --status [--json]
        heavy suite (tests, build) in one of the host's capped slots (L131);
  ameesh set <agent> model=… effort=… tier=… session_policy=par-lot|taille|jamais
        context_max_tokens=15M|0 turn_max_seconds=30m|2h|0 turn_mail_max=20|0
        turn_grace_seconds=20m|0
        run settings, effective from the next turn;
  ameesh alerts [--follow] [--json]     operational alerts (one object per line);
  ameesh notify [--once] [--dry-run] [--interval S] [--json] | --test human:ID
        alerts PUSHED to the responsible human (desktop, ntfy, Slack;
        `notify` key of the host configuration; L38);
  ameesh restart <agent> --brief FILE|-   fresh session on a brief;
  ameesh adopt <agent> --session ID --harness claude|codex|deepseek [--account C]
        [--cwd D] [--brief FILE|-] [--force]
        an existing (closed) interactive session moves under the runner (L39);
  ameesh resume <agent> [--brief FILE|-] [--fresh]
        restarts a stopped, dead or idle agent: same session if the next
        turn's account can resume it, otherwise a deterministic resume brief;
  ameesh interrupt <agent> <message…>   direct interruption (authorized senders);
  ameesh progress [--json] [--html FILE | --serve [--port 8765]] [--project P] [--since 24h]
        progress: work items, agents, milestones, budget (schema ameesh-progress/1);
        --serve: the page served locally, read-only, with a Refresh button (L304);
  ameesh serve [--port 8780] [--token-file F] [--human human:ID] | --openapi
        the read API /api/v1 on 127.0.0.1, Bearer token in a 0600 file (L84,
        docs/api/openapi-v1.json);
  ameesh projects [--json] [--project P] [--all]
        active projects: agents, state, current work item, unread, 24 h spend,
        plan or token, work items without an agent (schema ameesh-projects/1, L62);
  ameesh plan add "what" --pour DATE [--projet P] [--lot N] [--source S] | list
        | accept <id> | done <id> | cancel <id> | propose [--record] | show [--json]
        roadmap: dated commitments, proposals drawn from decisions,
        text Gantt (schema ameesh-roadmap/1, L96); planned dates of a
        task or a card: `ameesh work plan <id> --debut DATE --fin DATE --livraison DATE`;
  ameesh work estimates [--app P] [--json]
        L157: actual/estimated gaps of delivered work items (median, p80) by
        type and by estimate author; every work item carries an estimated
        duration from creation (`work add --estimate 2h`, `work plan <id> --estimate 90m`);
  ameesh fil list | show <project> [<lot>] [--last N] | tail <project> [<lot>]
        readable threads: every message that went through ameesh, in clear (R12);
  ameesh watch enable --agent A [--every 30m] | disable --agent A | show [--json]
        L121: the watch of an agent, a timer on this host that sends it the
        versioned instruction (consignes/veille.md), shown in `ameesh projects`;
  ameesh receipt verify | authenticator list   approval receipts (spec §8);
  ameesh action propose|show|list|request|approve|execute|reconcile|retry|replace|cancel
        gated actions (spec §7);
  ameesh decisions [--all] [--for human:ID] [--json]
        decisions waiting for a human, across all projects: agent requests
        (L124, oldest first), gated actions; apart, decisions written in prose
        in mail and work items waiting with no question (L233, not requests);
  ameesh decide ask (--lot <id|ref> | --new-lot "title") --question "…"
        --option a="…" [--option b="…"] [--recommend a --why "…"] [--urgent]
        [--by 2h] [--needs-signature] [--from-mail <id>]
        an agent asks the owner for a decision (work item in waiting_human,
        the only way in — L233);
  ameesh decide <id> <option | "free text"> [--key FILE | --signed]
        | withdraw <id> | show <id>
        the human's answer (never an agent's), sent back to the requester by
        mail; withdrawal by the requester; detail and proof (L124);
  ameesh chat [--harness claude|codex] [--instruction] [--dry-run]
        the owner's conversation agent: decisions of all projects, answers,
        requests forwarded to orchestrators; never takes another agent's
        session (L123);
  ameesh session list|backup|restore <agent>   off-device session backups (L53);
  ameesh approve-check [--url U] [--json]
        RP ID/origin agreement between ameesh-approve and this verifier;
  ameesh import-v0 | export-v0          switch from/to the v0 file mailbox;
  ameesh migrate | doctor     schema and diagnostics;
  ameesh canon check|show|sync          OKF canon: validation, cards, registry;
  ameesh memory status|consolidate|close <persona>   neutral persona memory (L54);
  ameesh module <name> …              installed project module commands;
  ameesh harness list|show|check|versions  harnesses (L16, L120);
  ameesh placement check [--agent A]    allowed or refused placements, and eligible ones;
  ameesh hosts [--json] [HOST]          host resources (L31);
  ameesh handoff plan AGENT --repository R [--dry-run] | show OPERATION
        reserved host move at a lot boundary, durable journal (L232a);
  ameesh menage [--apply] [--json]      cleanup of what agents create (L73);
  ameesh deps link|local|unlink|status|entries node_modules shared per lockfile (#248);
  ameesh conflicts [--all] [--json]     conflict radar between work items (L160b);
  ameesh guard check|run|drain          capacity guard of a lending VM (L159e);
  ameesh offload run [--profile P] [--fallback local|none] -- <command…>
        | status [<job>] | cancel <job>
        heavy suites run on a lending VM over SSH (L159b, AMEESH_OFFLOAD_HOST);
        same output and exit code, local fallback;
  ameesh sessions <persona> [--limit N] [--json]  current and past sessions (L52);
  ameesh agent spawn <name> --by <creator> --ttl <duration>   ephemeral agent;
  ameesh host enroll|revoke|list|show   enroll lending devices (L110,
        authorized human only);
  ameesh device enroll|challenge|show   device-side enrollment, in the VM (L110);
  ameesh serve --exec-only --auth-file FILE [--listen HOST:PORT] …
        mediated executor API (/api/exec/v1, L108): an executor on a
        device without database access (docs/EXECUTEUR-MEDIEE.md).

The human approval service (spec §9) is a separate command,
`ameesh-approve` (python -m ameesh.approve), run under its own
Unix user.

Nothing else: this file only routes to the modules, so that
`agent-mail` and `agent-runner` keep working exactly as before.
"""
from __future__ import annotations

import os
import sys

#: sous-commandes servies par mesh_cli (parseur déjà en place)
MESH_COMMANDS = (
    "list", "show", "set", "key", "approve", "approvals", "verify", "work", "cost",
    "migrate", "doctor", "import-v0", "export-v0", "canon", "placement", "agent",
    "review-class", "handoff",
    # L31 : ressources des hôtes (`ameesh hosts`)
    "hosts",
    # L73 : ménage de ce que les agents créent (`ameesh menage [--apply]`)
    "menage",
    # L160b : radar de conflits entre lots (`ameesh conflicts [--json]`)
    "conflicts",
    # L14 : le catalogue des modèles a son point d'entrée public, comme les autres
    # (`ameesh models list|show|discover`) — sans cette ligne, la commande sortait en
    # code 2 « sous-commande inconnue » AVANT toute base (revue B5).
    "models",
    # L16 : descripteurs de harnais (`ameesh harness list|show|check`)
    "harness",
    # L30 : comptes multiples par fournisseur (`ameesh accounts list|use|auto`)
    "accounts",
    # L70 : plafonds de budget du mesh, en base (`ameesh budget [set|unset]`)
    "budget",
    # L131 : suites lourdes plafonnées par hôte (`ameesh heavy -- <commande>`)
    "heavy",
)
#: exploitation (L26) : alertes, redémarrage sur brief, interruption directe
EXPLOITATION_COMMANDS = ("alerts", "restart", "interrupt")
#: adoption et reprise (L39, décision 0030)
REPRISE_COMMANDS = ("adopt", "resume")
#: sous-commandes des reçus d'approbation (receipts_cli)
RECEIPT_COMMANDS = ("receipt", "authenticator")
#: actions sous porte et file des décisions (actions_cli)
ACTION_COMMANDS = ("action", "decisions")


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(argv)
    except BrokenPipeError:
        # `ameesh alerts | head` : le lecteur a fermé la sortie, ce n'est pas
        # une erreur. Seul ce cas est absorbé ; un tube cassé ailleurs (harnais,
        # socket) remonte tel quel.
        if not _stdout_closed():
            raise
        # la sortie restante irait au tube fermé, et Python le signalerait
        # encore à l'arrêt : on la jette
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141  # 128 + SIGPIPE, comme le shell


def _stdout_closed() -> bool:
    """Le lecteur de la sortie standard l'a-t-il fermée ? `poll` sur le
    descripteur d'abord (POLLERR sur un tube sans lecteur) : après l'échec d'un
    `print`, le tampon peut déjà être vide et `flush()` réussir quand même."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):
        fd = None
    if fd is not None:
        try:
            import select
            poller = select.poll()
            poller.register(fd, select.POLLOUT)
            if any(ev & (select.POLLERR | select.POLLHUP) for _, ev in poller.poll(0)):
                return True
        except (AttributeError, OSError, ValueError):
            pass  # pas de poll (Windows) : le repli suffit
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        return True
    return False


def _dispatch(argv: list[str] | None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "module":
        from . import modules
        return modules.main(rest)
    if command in ("--version", "version"):
        from . import version
        print("ameesh %s" % version())
        return 0
    if command == "mail":
        from . import cli
        return cli.main(rest)
    if command == "run":
        from . import runner
        return runner.main(rest)
    if command == "attach":
        from . import runner
        return runner.attach_main(rest)
    if command == "activity":
        from . import activity
        return activity.main(rest)
    if command == "progress":
        from . import progress
        return progress.main(rest)
    if command == "deps":
        # #248: node_modules shared per lockfile, without database
        from . import deps
        return deps.main(rest)
    if command == "projects":
        from . import projects
        return projects.main(rest)
    if command == "serve":
        # L84: read API, local mode
        from . import serve
        return serve.main(rest)
    if command == "plan":
        # L96 : feuille de route (engagements, propositions, Gantt)
        from . import roadmap
        return roadmap.main(rest)
    if command == "memory":
        from . import persona_memory
        return persona_memory.main(rest)
    if command == "fil":
        from . import fil
        return fil.main(rest)
    if command == "sessions":
        # L52 (0032 §2): current and past persona sessions.
        from . import persona_sessions
        return persona_sessions.main(rest)
    if command == "watch":
        # L121: the watch of an agent (timer + versioned instruction)
        from . import watch
        return watch.main(rest)
    if command == "notify":
        # L38 (0030) : l'envoi des alertes au responsable humain
        from . import notify
        return notify.main(rest)
    if command == "decide":
        # L124 : demandes de décision au propriétaire, et ses réponses
        from . import decisions
        return decisions.main(rest)
    if command == "session":
        from . import session_backup
        return session_backup.main(rest)
    if command == "chat":
        # L123 : l'agent de conversation du propriétaire
        from . import chat
        return chat.main(rest)
    if command in EXPLOITATION_COMMANDS:
        from . import exploitation
        return exploitation.main(argv)
    if command in REPRISE_COMMANDS:
        from . import reprise
        return reprise.main(argv)
    if command in MESH_COMMANDS:
        from . import mesh_cli
        return mesh_cli.main(argv)
    if command in RECEIPT_COMMANDS:
        from . import receipts_cli
        return receipts_cli.main(argv)
    if command in ACTION_COMMANDS:
        from . import actions_cli
        return actions_cli.main(argv)
    if command == "serve":
        # L108 : l'API d'exécuteur médiée ; L84 y ajoutera l'interface
        from .mediated_executor import server
        return server.main(rest)
    if command in ("host", "device"):
        # L110 : enrôlement et identité des exécuteurs médiés
        from . import host_cli
        return host_cli.main(argv)
    if command == "guard":
        # L159e : garde de capacité d'une VM prêteuse (service système, sans base)
        from . import guard
        return guard.main(rest)
    if command == "offload":
        # L159b: heavy suites offloaded to a lending VM (mode b1)
        from . import offload
        return offload.main(rest)
    if command == "approve-check":
        from . import approve_check
        return approve_check.main(rest)
    print("ameesh: unknown subcommand %r" % command, file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
