#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh — la commande du mesh, et ses sous-commandes.

  ameesh mail <send|inbox|list|status|alias|hook|statusline|whoami>
        la boîte aux lettres ; `agent-mail` reste un alias (les hooks des
        harnais l'appellent) ;
  ameesh mail <bind|unbind|bindings>
        liaison explicite d'une session externe à un agent (L41, 0030) ;
  ameesh run [options]        l'exécuteur de la machine ; `agent-runner` reste
        un alias ;
  ameesh attach <agent> [--wait] [--ttl S]
        une session interactive sur le bail de l'agent, même session (C9) ;
  ameesh list | show          observabilité (hôte, bail, non-lus, budget, clé) ;
  ameesh key …                clés publiques (propriétaire / agent) ;
  ameesh approve | approvals | verify   approbations signées ;
  ameesh work …               lots (work_items) ;
  ameesh cost …               coût des tours, usage par tour, jauges et leur
        historique, solde du fournisseur payé au token (`cost turns|gauges|balance`) ;
  ameesh accounts list | use <harnais> <compte> | auto [harnais]
        comptes multiples par fournisseur : actif, jauges, forçage (L30) ;
  ameesh set <agent> model=… effort=… tier=… session_policy=par-lot|taille|jamais
        réglages d'exécution, effet au prochain tour ;
  ameesh alerts [--follow] [--json]     alertes d'exploitation (un objet par ligne) ;
  ameesh notify [--once] [--dry-run] [--interval S] [--json] | --test human:ID
        alertes POUSSÉES à l'humain responsable (bureau, ntfy, Slack ;
        clé `notify` de la configuration de l'hôte ; L38) ;
  ameesh restart <agent> --brief FICHIER|-   session neuve sur un brief ;
  ameesh adopt <agent> --session ID --harness claude|codex|deepseek [--account C]
        [--cwd D] [--brief FICHIER|-] [--force]
        une session interactive existante (fermée) passe sous l'exécuteur (L39) ;
  ameesh resume <agent> [--brief FICHIER|-] [--fresh]
        relance un agent arrêté, mort ou au repos : même session si le compte
        du prochain tour peut la reprendre, sinon brief de reprise déterministe ;
  ameesh interrupt <agent> <message…>   interruption directe (expéditeurs habilités) ;
  ameesh progress [--json] [--html FICHIER] [--project P] [--since 24h]
        avancement : lots, agents, jalons, budget (schéma ameesh-progress/1) ;
  ameesh fil list | show <projet> [<lot>] [--last N] | tail <projet> [<lot>]
        les fils lisibles : tout message passé par ameesh, en clair (R12) ;
  ameesh receipt verify | authenticator list   reçus d'approbation (spec §8) ;
  ameesh action propose|show|list|request|approve|execute|reconcile|retry|replace|cancel
        actions sous porte (spec §7) ;
  ameesh decisions [--for human:ID]     décisions qui attendent un humain (C10) ;
  ameesh approve-check [--url U] [--json]
        concordance RP ID/origines entre ameesh-approve et ce vérificateur ;
  ameesh import-v0 | export-v0          bascule depuis/vers la boîte fichier v0 ;
  ameesh migrate | doctor     schéma et diagnostic ;
  ameesh canon check|show|sync          canon OKF : validation, fiches, registre ;
  ameesh harness list|show|check        descripteurs de harnais (L16) ;
  ameesh placement check [--agent A]    placements admis ou refusés, et admissibles ;
  ameesh hosts [--json] [HÔTE]          ressources des hôtes (L31) ;
  ameesh sessions <persona> [--json]    ses sessions, présentes et passées (L52) ;
  ameesh sessions open <persona> --lot N   session parallèle sur un lot (L52b) ;
  ameesh agent spawn <nom> --by <créateur> --ttl <durée>   agent éphémère.

Le service d'approbation humaine (spec §9) est une commande séparée,
`ameesh-approve` (python -m ameesh.approve), lancée sous son propre
utilisateur Unix.

Rien d'autre : ce fichier ne fait que router vers les modules, pour que
`agent-mail` et `agent-runner` continuent de fonctionner à l'identique.
"""
from __future__ import annotations

import os
import sys

#: sous-commandes servies par mesh_cli (parseur déjà en place)
MESH_COMMANDS = (
    "list", "show", "set", "key", "approve", "approvals", "verify", "work", "cost",
    "migrate", "doctor", "import-v0", "export-v0", "canon", "placement", "agent",
    "review-class",
    # L31 : ressources des hôtes (`ameesh hosts`)
    "hosts",
    # L14 : le catalogue des modèles a son point d'entrée public, comme les autres
    # (`ameesh models list|show|discover`) — sans cette ligne, la commande sortait en
    # code 2 « sous-commande inconnue » AVANT toute base (revue B5).
    "models",
    # L16 : descripteurs de harnais (`ameesh harness list|show|check`)
    "harness",
    # L30 : comptes multiples par fournisseur (`ameesh accounts list|use|auto`)
    "accounts",
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
    if command == "mail":
        from . import cli
        return cli.main(rest)
    if command == "run":
        from . import runner
        return runner.main(rest)
    if command == "attach":
        from . import runner
        return runner.attach_main(rest)
    if command == "progress":
        from . import progress
        return progress.main(rest)
    if command == "fil":
        from . import fil
        return fil.main(rest)
    if command == "sessions":
        # L52 (0032 §2) : les sessions d'une persona, présentes et passées
        from . import persona_sessions
        return persona_sessions.main(rest)
    if command == "notify":
        # L38 (0030) : l'envoi des alertes au responsable humain
        from . import notify
        return notify.main(rest)
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
    if command == "approve-check":
        from . import approve_check
        return approve_check.main(rest)
    print("ameesh : sous-commande inconnue %r" % command, file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
