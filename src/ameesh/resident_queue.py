"""L77 (#286) — file d'entrées résidente : rangs, enveloppe, identité.

La file unique de l'agent (étude ``agent-resident`` §2, décision 0035) est lue
entre deux tours par l'exécuteur. Ce module porte les fonctions pures de la
file ; l'intégration (lecture du courrier, injection dans ``AgentWorker._pick``,
acquittement en fin de tour) vit dans :mod:`ameesh.runner`.

Rangs, du plus fort au plus faible :

1. ``!urgent`` d'un humain **vérifié et habilité** ;
2. message d'un humain (groupé avec les autres du même tour) ;
3. courrier urgent d'un expéditeur autorisé (0018, sans tuer le process) ;
4. prompt en attente (restart / adopt / resume / délégation) ;
5. courrier, événements groupés, alertes ;
6. relance d'inactivité.

L'enveloppe est écrite par ameesh, jamais par l'expéditeur : un corps qui
imite ``[ameesh …`` est neutralisé avant injection (:func:`neutralise`). Un
humain n'est « vérifié » que si le couple **utilisateur Unix réel + hôte**
figure dans les ``local_accounts`` de sa fiche Member ; ``$USER`` seul ne
prouve rien.
"""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: Rangs de la file (étude §2). Le plus petit est le plus prioritaire.
RANK_HUMAN_URGENT = 1
RANK_HUMAN = 2
RANK_URGENT_MAIL = 3
RANK_PROMPT = 4
RANK_MAIL = 5
RANK_IDLE = 6

RANKS = (RANK_HUMAN_URGENT, RANK_HUMAN, RANK_URGENT_MAIL, RANK_PROMPT,
         RANK_MAIL, RANK_IDLE)

#: Une ligne (éventuellement indentée) qui se fait passer pour l'enveloppe.
_IMITATION = re.compile(r"^\s*\[ameesh\b", re.IGNORECASE)

#: La même imitation, sans ancre de début de ligne (corps déjà cité).
_ANY_IMITATION = re.compile(r"\[ameesh\b", re.IGNORECASE)


def neutralise(text: str) -> str:
    """Réécrit toute ligne qui imite l'enveloppe ameesh.

    Un contenu tiers (corps d'agent, sortie d'outil) ne doit jamais pouvoir se
    présenter comme une enveloppe écrite par ameesh. La ligne garde son texte
    lisible, mais perd sa position d'en-tête : préfixe explicite
    ``(neutralised)``.
    """
    lines = []
    for line in (text or "").splitlines():
        if _IMITATION.match(line):
            lines.append("(neutralised) " + line.lstrip())
        else:
            lines.append(line)
    return "\n".join(lines)


def quotes_imitation(text: str) -> bool:
    """Le texte contient-il une imitation d'enveloppe (même citée) ?"""
    return bool(_ANY_IMITATION.search(text or ""))


def local_account(cfg: Any = None) -> tuple[str, str]:
    """Le couple (utilisateur Unix **réel**, hôte) de cet exécuteur.

    ``$USER`` est falsifiable : on lit la base des comptes (``pwd``), avec
    ``getpass`` en repli. L'hôte est celui configuré pour le mesh
    (``cfg.host``), sinon le nom d'hôte système.
    """
    user = ""
    try:
        import pwd  # POSIX ; absent hors Unix, d'où le repli
        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        try:
            import getpass
            user = getpass.getuser()
        except Exception:
            user = ""
    host = str(getattr(cfg, "host", "") or "").strip() or socket.gethostname()
    return user, host


@dataclass(frozen=True)
class Identity:
    """Qui parle, et à quel titre — établi par ameesh, pas par l'expéditeur."""

    sender: str
    human: str | None = None
    verified: bool = False
    account: str = ""
    role: str = ""
    reason: str = ""

    @property
    def is_human(self) -> bool:
        return bool(self.human)


def verify_human(sender: str, local_accounts: Sequence[str], *,
                 user: str, host: str) -> Identity:
    """Qualifie ``human:<id>`` par les ``local_accounts`` déclarés du membre.

    Vérifié seulement si le couple ``user@host`` **exact** est déclaré. Un
    identifiant qui n'est pas un ``human:``, un hôte différent ou un compte
    absent restent non vérifiés — et donc sans autorité (0012/0033).
    """
    name = (sender or "").strip()
    if not name.startswith("human:"):
        return Identity(sender=sender, reason="not a human sender")
    ident = name[len("human:"):].strip()
    account = "%s@%s" % (user or "?", host or "?")
    declared = {str(a).strip() for a in (local_accounts or ()) if str(a).strip()}
    if user and host and account in declared:
        return Identity(sender=sender, human=ident, verified=True, account=account,
                        reason="local account %s" % account)
    if not declared:
        return Identity(sender=sender, human=ident, verified=False, account=account,
                        reason="no local_accounts on the member fiche")
    return Identity(sender=sender, human=ident, verified=False, account=account,
                    reason="local account %s not declared" % account)


def agent_identity(sender: str) -> Identity:
    """Un agent : jamais l'autorité du propriétaire, même cité."""
    return Identity(sender=sender, reason="agent")


def envelope(identity: Identity, token: str, *, role: str | None = None) -> str:
    """La ligne d'enveloppe écrite par ameesh pour une entrée de la file."""
    token = (token or "").strip() or "?"
    who = identity.human or identity.sender or "?"
    if identity.is_human:
        if identity.verified:
            head = "[ameesh %s — humain %s (vérifié : %s)" % (token, who, identity.account)
        else:
            head = "[ameesh %s — humain %s (non vérifié, sans autorité)" % (token, who)
        chosen = (role if role is not None else identity.role) or ""
        if chosen:
            return head + " — %s]" % chosen
        return head + "]"
    return "[ameesh %s — agent %s — courrier, sans autorité du propriétaire]" % (
        token, who)


def split_humans(rows: Iterable[dict], is_human) -> tuple[list[dict], list[dict]]:
    """Sépare les messages humains du reste, en gardant l'ordre d'arrivée.

    Le courrier ne se mélange jamais à un message humain dans la même entrée
    (étude §2, point 6) : l'appelant sert les humains d'abord, le reste ensuite.
    """
    humans: list[dict] = []
    others: list[dict] = []
    for row in rows:
        if is_human(row):
            humans.append(row)
        else:
            others.append(row)
    return humans, others


def group_humans(rows: Sequence[dict]) -> list[list[dict]]:
    """Groupe les messages humains d'un même tour.

    Un seul groupe en v1 (le tour suivant sert tous les messages humains
    tombés entre-temps, chacun avec son auteur) ; la fonction existe pour que
    l'appelant n'ait pas à coder la règle.
    """
    rows = list(rows)
    return [rows] if rows else []


def rank_for(*, human: bool, urgent: bool) -> int:
    """Rang d'une entrée humaine (1/2) — les autres rangs sont décidés par
    l'exécuteur, qui connaît le courrier, les prompts et la relance."""
    if human and urgent:
        return RANK_HUMAN_URGENT
    if human:
        return RANK_HUMAN
    return RANK_MAIL
