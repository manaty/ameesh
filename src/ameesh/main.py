#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh — the mesh command, and its subcommands.

  ameesh accounts list | use <harness> <account> | auto [harness]
        multiple accounts per provider: active, gauges, override (L30);
  ameesh action propose|show|list|request|approve|execute|reconcile|retry|replace|cancel
        gated actions (spec §7);
  ameesh activity <persona> [--since 24h] [--json]   persona activity log (L59);
  ameesh adopt <agent> --session ID --harness claude|codex|deepseek [--account C]
        [--cwd D] [--brief FILE|-] [--force]
        an existing (closed) interactive session moves under the runner (L39);
  ameesh agent move NOM --to HOST [--keep-session]   manual move of a pinned agent
        to another admitted host, without a live lease when the source executor
        is stopped (#3299);
  ameesh agent peek <name> [--limit N] [--json]   responsible human's work summary;
  ameesh agent retire <name> --reason R | restore <name>   take an agent out of the
        active registry (never designated, claimed nor mailed), or back (#3460);
  ameesh agent spawn <name> --by <creator> --ttl <duration>   ephemeral agent;
  ameesh alerts [--follow] [--json]     operational alerts (one object per line);
  ameesh approvals            list signed approvals;
  ameesh approve              sign an approval;
  ameesh approve-check [--url U] [--json]
        RP ID/origin agreement between ameesh-approve and this verifier;
  ameesh attach <agent> [--wait] [--ttl S] [--max 2h]
        bounded native access: sleep the resident, then return its lease (L82);
  ameesh authenticator list   registered authenticators (spec §8);
  ameesh budget [--json] | set --per-hour X [--per-day Y] [--agent A]
        | unset [--per-hour] [--per-day] [--agent A]
        mesh budget caps, stored in the database, reloaded live (L70);
  ameesh canon check|show|sync          OKF canon: validation, cards, registry;
  ameesh chat [--harness claude|codex] [--instruction] [--dry-run]
        the owner's conversation agent: decisions of all projects, answers,
        requests forwarded to orchestrators; never takes another agent's
        session (L123);
  ameesh ci status <lot> [--limit N] [--json]
        latest CI runs and failing tests of a {work};
  ameesh conflicts [--all] [--json]     conflict radar between {works} (L160b);
  ameesh context corpus export --out FILE [--days N | --since T] [--harness H]
        | check FILE
        read-only replay corpus (`ameesh-context-corpus/1`) rebuilt from the
        turn ledger and `turn.context` events; written outside the repository,
        mode 0600 (#5134);
  ameesh context replay --corpus FILE --candidates FILE --out FILE [--force]
        counterfactual replay of the context policy over that corpus: observed
        and counterfactual decisions kept apart, nothing activated (#5135);
  ameesh cost …               turn cost, per-turn usage, gauges and their
        history, pay-per-token provider balance (`cost turns|gauges|balance`);
  ameesh debit [--json] [--check] [--hours N] [--threshold 2h]
        débit mesuré sur les jalons (canon 24/10/2026) : les {works} en qa/blocked par
        âge, avis dus par relecteur, avis bloquants non traités, holds et
        réveils, agents au repos avec du travail dû, tours et fusions par
        heure, durée de CI par job ; `--check` sort 2 sur une attente > seuil ;
  ameesh decide <id> <option | "free text"> [--key FILE | --signed]
        | withdraw <id> | show <id>
        the human's answer (never an agent's), sent back to the requester by
        mail; withdrawal by the requester; detail and proof (L124);
  ameesh decide ask (--lot <id|ref> | --new-lot "title") --question "…"
        --option a="…" [--option b="…"] [--recommend a --why "…"] [--urgent]
        [--by 2h] [--needs-signature] [--from-mail <id>]
        an agent asks the owner for a decision ({work} in waiting_human,
        the only way in — L233);
  ameesh decisions [--all] [--for human:ID] [--json]
        decisions waiting for a human, across all projects: agent requests
        (L124, oldest first), gated actions; apart, decisions written in prose
        in mail and {works} waiting with no question (L233, not requests);
  ameesh deps link|local|unlink|status|entries node_modules shared per lockfile (#248);
  ameesh device enroll|challenge|show   device-side enrollment, in the VM (L110);
  ameesh doctor               schema and diagnostics;
  ameesh events [--after ID] [--limit N] [--json] | prune [--days N] [--dry-run]
        the event log ({work} transitions, turns, costs, threads, decisions, alerts) and
        its retention (365 days by default, L85);
  ameesh export-v0            export to the v0 file mailbox;
  ameesh fil list | show <project> [<lot>] [--last N] | tail <project> [<lot>]
        readable threads: every message that went through ameesh, in clear (R12);
  ameesh github gestes [--since 24h] [--agent A] [--match 'run cancel'] [--json]
        | identity <agent> [--json]
        the gh commands run by agents, journaled per turn (who cancelled,
        merged, labelled), and the GitHub identity an agent resolves to (#1645);
  ameesh github read <chemin> [--ttl SECONDES]
        a GitHub REST read for the agents' watch scripts, through the host's
        shared reads cache (#848, #860): a conditional request costs no quota,
        and exit code 3 says the quota is exhausted with nothing cached.
  ameesh guard check|run|drain          capacity guard of a lending VM (L159e);
  ameesh handoff plan AGENT --repository R [--dry-run] | show OPERATION
        reserved host move at a {work} boundary, durable journal (L232a);
  ameesh harness adopt|tick|show-adoption|rollback-adoption   adoption of a harness or model version
        (#3264 C): auto at equal price after a canary, owner decision otherwise;
  ameesh harness list|show|check|versions  harnesses (L16, L120);
  ameesh heavy [--nice N] -- <command> | --status [--json]
        heavy suite (tests, build) in one of the host's capped slots (L131);
  ameesh host drain HOST [--for 8h] | undrain HOST | reconcile HOST | status [HOST]
        durable withdrawal, return and verified lost-host recovery (L232e);
  ameesh host enroll|revoke|list|show   enroll lending devices (L110,
        authorized human only);
  ameesh hosts [--json] [HOST]          host resources (L31);
  ameesh hosts HOST --hold --until 2h --reason R | --release
                                        the owner holds a host on purpose: its
                                        agents' alerts stay quiet (#254);
  ameesh housekeeping [--apply] [--json]  cleanup of what agents create (L73);
  ameesh identity verify [JETON|-] [--issuer U --client-id C] [--domain D]
                         [--binding subject|email]
        vérifie un jeton d'identité OIDC et le rattache au canon (le couple
        `{{issuer, subject}}` d'une fiche Member par défaut, `emails` sur choix
        explicite) : L86, {work} #377 ; sortie 3 si aucun membre ne correspond ;
  ameesh import-v0            import from the v0 file mailbox;
  ameesh interrupt <agent> <message…>   direct interruption (authorized senders);
  ameesh key …                public keys (owner / agent);
  ameesh list                 agents, hosts, leases, unread messages and budgets;
  ameesh mail <bind|unbind|bindings>
        explicit binding of an external session to an agent (L41, 0030);
  ameesh mail <send|show|inbox|list|status|alias|hook|statusline|whoami>
        the mailbox; `agent-mail` remains an alias (harness hooks call it);
        `send` refuses an unknown recipient (close names suggested) or a
        stopped one (unless --queue);
  ameesh mail forward <old> <new> [--dry-run]
        re-delivers the pending mail of a stopped agent to a live agent;
  ameesh memory status|consolidate|close <persona>   neutral persona memory (L54);
  ameesh migrate              apply schema migrations;
  ameesh models list|show|discover      model catalog and discovery;
  ameesh module <name> …              project module commands (Pipeline: adopt/advance/hold/release), --list
                                      for installation evidence;
  ameesh module dev sync-merges | sync-github | project-github | merge-pr
        | review-class    development tooling (L100 T5); `work sync-merges…`
        and `review-class` stay aliases without a removal date;
  ameesh modules [--json]             discovered modules (built in and ameesh.modules
                                      entry points): version, capabilities (L98);
  ameesh notify [--once] [--dry-run] [--interval S] [--json] | --test human:ID
        alerts PUSHED to the responsible human (desktop, ntfy, Slack;
        `notify` key of the host configuration; L38);
  ameesh offload run [--profile P] [--fallback local|none] -- <command…>
        | status [<job>] | cancel <job>
        heavy suites run on a lending VM over SSH (L159b, AMEESH_OFFLOAD_HOST);
        same output and exit code, local fallback;
  ameesh placement check [--agent A]    allowed or refused placements, and eligible ones;
  ameesh plan <accept|add|cancel|done|export|get|import|list|propose|show|update|validate>
        add "what" --due DATE [--proposed] [--key K]; update eN;
        import FILE [--apply] [--update]; export [--project P]; validate [FILE];
        roadmap: dated commitments, proposals drawn from decisions,
        text Gantt (schema ameesh-roadmap/1, L96); planned dates of a
        task or a card: `ameesh work plan <id> --start DATE --end DATE --delivery DATE`;
  ameesh process check <page.md> [--ref REF] [--version N] [--json]
        compile the workflow block of a canon Process card (#2225);
  ameesh process-approvals refresh|check|show
        GitHub approval bridge (#4006): bureau reader on bureau-goal,
        fail-closed local projection for the hosts; source=api is never an
        authority;
  ameesh process-observations refresh|check|show|revoke
        observations journal (#4425): the bureau collects staging
        attestations and approvals, the hosts read a fresh fail-closed
        projection; deployed never expires by age, QA observations do;
  ameesh progress [--json] [--html FILE | --serve [--port 8765]] [--project P] [--since 24h]
        progress: {works}, agents, milestones, budget (schema ameesh-progress/1);
        --serve: the page served locally, read-only, with a Refresh button (L304);
  ameesh projects [--json] [--project P] [--all]
        active projects: agents, state, current {work}, unread, 24 h spend,
        plan or token, {works} without an agent (schema ameesh-projects/1, L62);
  ameesh provenance record --commit SHA | statement | intent | created | install | show
        #1465 : preuve exacte d'origine (acteur, session, tour, harnais) d'un
        commit ou d'un jalon — le hook `pre-commit` atteste le commit qui vient,
        le hook `post-commit` constate l'objet né puis écrit la preuve ;
        record --signed : hors runner (bureau/owner), attestation signée par une
        clé owner (#4485), sans PID local ni ameesh sur l'hôte du commit ;
  ameesh receipt verify       approval receipts (spec §8);
  ameesh release open|check|reason <lot>   release perimeter and validation;
  ameesh replay bench|run|report   banc de rejeu comparatif (#3264 B, L15) :
        trois {works} {delivered} sur leur base gelée, version installée et version
        évaluée, relecture croisée à l'aveugle ;
  ameesh restart <agent> --brief FILE|-   fresh session on a brief;
  ameesh resume <agent> [--brief FILE|-] [--fresh]
        restarts a stopped, dead or idle agent: same session if the next
        turn's account can resume it, otherwise a deterministic resume brief;
  ameesh review-class [--project P] [--diff REF | FILE ...]
        review class of the changed files from the project's policy;
  ameesh role canary [--json]   L165 (EP7 P3) : geste sans effet gardé par
        l'époque du bail de l'environnement (AMEESH_ROLE + AMEESH_LEASE_EPOCH,
        ou session liée) ; 0 accepté (un `pilot.gesture` canary), 1 refusé
        (rien journalisé ; détenteur nommé pour une époque périmée ou
        expirée), 2 aucun bail à garder ;
  ameesh role run <agent> [--min-left S] [--dry-run] [--json] -- <commande…>
        L165 (EP5) : une minuterie mécanique ne tourne que sur l'hôte qui
        détient le bail vivant de ce rôle (composeur, veille GitHub, guet CI) ;
        l'époque du bail est passée à la commande pour la garde EP4 ;
  ameesh role status [ROLE] [--json]   elected roles: holder, epoch, ranked
                                        candidates (L165); the runners publish;
  ameesh role timers [--role R] [--install|--check|--remove] [--no-apply] [--no-sync] [--json]
        L165 (EP5) : pose (ou vérifie) les minuteries mécaniques de la veille
        GitHub sur cet hôte, s'il est candidat du rôle élu ; sans clé `github`,
        les passes CI et issues sont refusées explicitement (§9) ;
  ameesh run [options]        the machine's runner; `agent-runner` remains
        an alias;
  ameesh sandbox list [--agent NAME] [--state STATE] [--scope DIGEST] [--limit N] [--json]
  ameesh sandbox show TURN_ID [--json]
  ameesh serve --exec-only --auth-file FILE [--listen HOST:PORT] …
        mediated executor API (/api/exec/v1, L108): an executor on a
        device without database access (docs/EXECUTEUR-MEDIEE.md).
  ameesh serve [--port 8780] [--token-file F] [--human human:ID] | --openapi
        the read API /api/v1 on 127.0.0.1, Bearer token in a 0600 file (L84,
        docs/api/openapi-v1.json); GET /api/v1/events streams the event log (L85);
  ameesh service install|uninstall|status [--agent A]... [--no-runner]
        [--no-notify] [--dry-run] [--json]
        user services (launchd on macOS, L65): the host runner and
        `ameesh notify`, or one runner per agent; `status` reads launchd;
  ameesh session list|backup|restore <agent>   off-device session backups (L53);
  ameesh sessions <persona> [--limit N] [--json]  current and past sessions (L52);
  ameesh set <agent> model=… effort=… tier=… session_policy=par-lot|taille|jamais
        context_max_tokens=15M|0 session_max_tokens=1M turn_reread_budget=1.5M|0
        session_reread_budget=40M|0 turn_max_seconds=30m|2h|0 turn_mail_max=20|0
        turn_grace_seconds=20m|0
        run settings, effective from the next turn;
  ameesh show <agent>         detailed agent observability;
  ameesh turn current [--local] [--expect-turn ID] [--json]
        read the calling turn; reuse requires its captured turn id;
  ameesh turn observe (--turn ID | --request ID) [--json]
        #2167: a supervisor's read of one turn or request: states, accounting,
        `quiescent` (schema ameesh-turn-observation/1); never writes, exit 3 if unknown;
  ameesh veille scan [--dry-run] [--json] [--no-probe] [--harness ID]
        #3264 A: daily watch of harness/model versions; one evaluation {work} per
        harness, priority 1 on a vendor recommendation (config `veille.sources`);
  ameesh verify               verify a signed approval;
  ameesh version | --version  version of the installed package;
  ameesh watch enable --agent A [--every 30m] | disable --agent A | show [--json]
        L121: the watch of an agent, a timer on this host that sends it the
        versioned instruction (consignes/veille.md), shown in `ameesh projects`;
  ameesh work estimates [--app P] [--json]
        L157: actual/estimated gaps of delivered {works} (median, p80) by
        type and by estimate author; every {work} carries an estimated
        duration from creation (`work add --estimate 2h`, `work plan <id> --estimate 90m`);
  ameesh work …               {works}; `work backlog add|list`: continuous
        improvement queue, picked up by idle agents (L119, 0037);
        `work unrecorded [--since 24h] [--json]`: human requests (mail,
        decision answers, chat) not attached to any {work} (L130);

The human approval service (spec §9) is a separate command,
`ameesh-approve` (python -m ameesh.approve), run under its own
Unix user.

Nothing else: this file only routes to the modules, so that
`agent-mail` and `agent-runner` keep working exactly as before.
"""
from __future__ import annotations

import os
import sys


def _doc(vocabulary=None) -> str:
    """``ameesh --help``: the description in the active module's vocabulary.

    L102 T2 (#4392): the core's neutral word is CORE ("tâche"); the dev tooling
    module declares DEV ("lot"), read through :func:`ameesh.vocab.active`. The
    text is a template (``{work}``/``{works}``/``{delivered}``) so the verbs
    and the agreement follow the vocabulary."""
    from . import vocab
    v = vocab.active() if vocabulary is None else vocabulary
    return __doc__.format(work=v.noun, works=v.plural,
                          delivered=v.agree("livré", "livrée", plural=True))


#: sous-commandes servies par mesh_cli (parseur déjà en place)
MESH_COMMANDS = (
    "list", "show", "set", "key", "approve", "approvals", "verify", "work", "cost",
    "migrate", "doctor", "import-v0", "export-v0", "canon", "placement", "agent",
    "review-class", "handoff",
    # L31 : ressources des hôtes (`ameesh hosts`)
    "hosts",
    # L73 : ménage de ce que les agents créent (`ameesh housekeeping [--apply]`)
    "housekeeping",
    # L148 : `ameesh menage` reste accepté (alias déprécié, retiré au stage 7)
    "menage",
    # L160b : radar de conflits entre lots (`ameesh conflicts [--json]`)
    "conflicts",
    # L14 : le catalogue des modèles a son point d'entrée public, comme les autres
    # (`ameesh models list|show|discover`) — sans cette ligne, la commande sortait en
    # code 2 « sous-commande inconnue » AVANT toute base (revue B5).
    "models",
    # L16 : descripteurs de harnais (`ameesh harness list|show|check`)
    "harness",
    # #3264 A : veille quotidienne des versions (`ameesh veille scan`)
    "veille",
    # #2167 B2 : observation d'un tour ou d'une requête (`ameesh turn observe`)
    "turn",
    "sandbox",
    # L30 : comptes multiples par fournisseur (`ameesh accounts list|use|auto`)
    "accounts",
    # L70 : plafonds de budget du mesh, en base (`ameesh budget [set|unset]`)
    "budget",
    # L131 : suites lourdes plafonnées par hôte (`ameesh heavy -- <commande>`)
    "heavy",
    # L161c (#744) : la CI lue par `ameesh ci status <lot>` — le parseur existait
    # dans mesh_cli, mais aucune liste de routage ne le servait : code 2 avant
    # même de regarder la base.
    "ci",
    # L750/L756 (#561/#566) : le périmètre d'une release et sa porte
    # (`ameesh release open|check|reason`).
    "release",
)
#: exploitation (L26) : alertes, redémarrage sur brief, interruption directe
EXPLOITATION_COMMANDS = ("alerts", "restart", "interrupt")
#: adoption et reprise (L39, décision 0030)
REPRISE_COMMANDS = ("adopt", "resume", "pause")
#: sous-commandes des reçus d'approbation (receipts_cli)
RECEIPT_COMMANDS = ("receipt", "authenticator")
#: actions sous porte et file des décisions (actions_cli)
ACTION_COMMANDS = ("action", "decisions")


def main(argv: list[str] | None = None) -> int:
    from .platform.child_environment import managed_entry
    if not managed_entry():
        return 1
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
        print(_doc())
        return 0
    command, rest = argv[0], argv[1:]
    if command == "role" and rest[:1] == ["run"]:
        # L165 (EP5) : minuterie gardée par le bail du rôle. Routé avant
        # `role status` (EP2) : les deux sous-commandes vivent dans `role`.
        from . import role_run
        return role_run.main(rest[1:])
    if command == "role" and rest[:1] == ["timers"]:
        # L165 (EP5) : pose des minuteries mécaniques sur un hôte candidat.
        from . import role_timers
        return role_timers.main(rest[1:])
    if command == "module":
        from . import modules
        return modules.main(rest)
    if command == "modules":
        # L98 (#4308) : lecture du registre, sans import d'implémentation.
        from .modules import registry
        return registry.main(rest)
    if command in ("--version", "version"):
        from . import version
        print("ameesh %s" % version())
        return 0
    if command == "turn" and rest[:1] == ["current"]:
        from . import turn_context
        return turn_context.cmd_current(rest[1:])
    if command == "mail":
        from . import cli
        return cli.main(rest)
    if command == "run":
        from . import runner
        return runner.main(rest)
    if command == "attach":
        from . import runner
        return runner.attach_main(rest)
    if command == "agent" and rest and rest[0] == "peek":
        from . import agent_peek
        return agent_peek.main(rest[1:])
    if command == "activity":
        from . import activity
        return activity.main(rest)
    if command == "github" and rest[:1] in (["gestes"], ["identity"]):
        # #1645 : journal des gestes gh des agents, identité GitHub par rôle ;
        # `ameesh github read` (#860) et l'aide restent à github_cli
        from . import github_identity
        return github_identity.main(rest)
    if command == "progress":
        from . import progress
        return progress.main(rest)
    if command == "debit":
        # canon 24/10/2026 : l'auditeur commence chaque passage par le débit,
        # mesuré sur les jalons (jamais sur les colonnes d'un lot).
        from . import debit
        return debit.main(rest)
    if command == "process":
        # #2225 : vérifier le bloc `workflow:` d'une carte Process du canon
        from . import process_card
        return process_card.main(rest)
    if command == "deps":
        # #248: node_modules shared per lockfile, without database
        from . import deps
        return deps.main(rest)
    if command == "projects":
        from . import projects
        return projects.main(rest)
    if command == "serve":
        if "--exec-only" in rest:
            # L108 : l'API d'exécuteur médiée (/api/exec/v1). `serve.main`
            # (L84) ne connaît pas --exec-only : le routeur doit trancher
            # avant, sinon le serveur médié reste inatteignable (#743).
            from .mediated_executor import server
            return server.main(rest)
        # L84: read API, local mode
        from . import serve
        return serve.main(rest)
    if command == "events":
        # L85: the event log of `GET /api/v1/events`
        from . import events
        return events.main(rest)
    if command == "context" and rest[:1] == ["corpus"]:
        # #5134 (CTX-05a) : corpus de rejeu, reconstruction en lecture seule.
        from . import context_corpus
        return context_corpus.main(rest[1:])
    if command == "context" and rest[:1] == ["replay"]:
        # #5135 (CTX-05b) : rejeu contrefactuel, rien n'est activé.
        from . import context_replay
        return context_replay.main(rest[1:])
    if command == "context" and rest[:1] == ["pilot"]:
        # #5250 (CTX-06) : pilote réversible de la politique d'un agent.
        from . import context_pilot
        return context_pilot.main(rest[1:])
    if command == "host" and rest and rest[0] in ("drain", "undrain", "reconcile", "status"):
        from . import host_lifecycle
        return host_lifecycle.main(rest)
    if command == "role":
        # L165 (EP2) : détenteur, époque et candidats classés d'un rôle élu ;
        # EP7 P3 (#3433) : `role canary`, le geste sans effet de l'exercice
        from . import role
        return role.main(rest)
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
    if command == "github":
        # #860 : lectures GitHub des veilles par le cache partagé de l'hôte
        from . import github_cli
        return github_cli.main(rest)
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
    if command == "service":
        # L65 : services utilisateur (launchd sur macOS)
        from . import service
        return service.main(rest)
    if command == "provenance":
        # #1465 : preuve exacte d'origine d'un commit ou d'un jalon
        from . import review_provenance
        return review_provenance.main(rest)
    if command == "decide":
        # L124 : demandes de décision au propriétaire, et ses réponses
        from . import decisions
        return decisions.main(rest)
    if command == "session":
        from . import session_backup
        return session_backup.main(rest)
    if command == "identity":
        # L86 (0033 §4) : identité des humains par OIDC et rattachement au canon
        from . import oidc
        return oidc.main(rest)
    if command == "chat":
        # L123 : l'agent de conversation du propriétaire
        from . import chat
        return chat.main(rest)
    if command == "replay":
        # #3264 B (L15) : le banc de rejeu comparatif, isolé et sans effet de
        # bord ; la sélection lit les lots, le rapport est un fichier.
        from . import replay
        return replay.main(rest)
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
    if command == "process-approvals":
        # #4006 : pont d'approbation GitHub (projection locale fail-closed)
        from . import process_approvals_cli
        return process_approvals_cli.main(rest)
    if command == "process-observations":
        # #4425 : journal d'observations IMA (journal append-only, projection
        # locale fail-closed)
        from . import process_observations_cli
        return process_observations_cli.main(rest)
    print("ameesh: unknown subcommand %r" % command, file=sys.stderr)
    print(_doc())
    return 2


if __name__ == "__main__":
    sys.exit(main())
