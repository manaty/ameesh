# SPDX-License-Identifier: AGPL-3.0-only
"""#2333 — canon §11 bis : tout passe par paquet, garde mécanique.

Décision de l'owner du 18/10/2026 vers 03:15 (canon home#107, §2/§11 bis) :
« tout passe par paquet ». Aucune fusion de lot directe sur la cible
d'intégration (`main` ameesh, `develop` Nexlink), et aucune CI `ci-lot` entre
deux paquets. Chaque lot entre par le paquet (`integration/**`).

Ce module porte la décision, une seule fois, pour les gestes qui pourraient
la contourner :

* `merge_pr.direct_merge` (`ameesh work merge-pr`) ;
* `fire.give` / `fire._act` (`ameesh work fire`, auto-fusion) ;
* `ci.cmd_ci_run` (`ameesh ci run --lot`, étiquette `ci-lot-<sha>`).

Une PR dont la tête est `integration/**` est un paquet : elle passe ; une
tête qui est elle-même une ref d'intégration (`release/*`) aussi. Toute
autre tête vers la cible d'intégration (`lot/**`, `agent/**`, `review/**`,
`fix/**`, `auditeur/**`…, tête illisible comprise) est refusée, sauf exception **déclarée dans la note du lot**
(`incident` : cible rouge ; `unblock` : déblocage de la CI). La déclaration
est un événement du journal, au format stable ::

    packet-exception incident sha=<40hex> reason=<motif>

`--exception incident|unblock --reason …` l'écrit (`declare`) ; la garde la
relit (`declared`) pour les passages d'auto-fusion, qui n'ont pas de ligne de
commande. Une déclaration ne vaut que pour la tête qu'elle nomme : une
nouvelle tête gelée demande une nouvelle exception.

Le refus est une `PacketGuardError` dont le message cite §11 bis et dit le
geste à faire. Il n'écrit rien : aucun commit, aucune étiquette.
"""
from __future__ import annotations

import re

from . import plan_git

#: tête d'un paquet : elle seule se fusionne hors paquet
PACKET_PREFIX = "integration/"
#: exceptions admises par le canon §11 bis (et §3)
EXCEPTIONS = ("incident", "unblock")
#: préfixe de la note-journal d'une exception
MARKER = "packet-exception"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DECLARATION = re.compile(
    r"^packet-exception (?P<exception>incident|unblock) "
    r"sha=(?P<sha>[0-9a-f]{40})(?: reason=(?P<reason>.*))?$")


class PacketGuardError(RuntimeError):
    """Le geste sort du paquet sans exception déclarée : refus actionnable."""


def branch_of(value) -> str:
    """La branche courte d'une ref (`origin/main`, `refs/heads/lot/x`)."""
    return plan_git.short_branch(value)


def is_work_branch(branch) -> bool:
    """La tête est-elle une branche de travail ? Le doute refuse : toute tête
    qui n'est ni un paquet (`integration/**`) ni elle-même une ref
    d'intégration (`main`, `develop`, `release/*` : remontée d'une release)
    en est une — `lot/**`, `agent/**`, `review/**`, mais aussi `fix/**`,
    `auditeur/**`, `remise/**`… et une tête illisible (vide)."""
    name = branch_of(branch)
    return not name or not (is_packet_branch(name) or plan_git.integration_branch(name))


def is_packet_branch(branch) -> bool:
    """La tête est-elle la branche d'un paquet (`integration/**`) ?"""
    return branch_of(branch).startswith(PACKET_PREFIX)


def is_integration_target(target) -> bool:
    """La cible est-elle une cible d'intégration (`main`, `develop`,
    `master`, `release/*`) ? Une cible vide est traitée comme telle par les
    gardes qui ne peuvent pas la résoudre : le doute refuse."""
    name = branch_of(target)
    return not name or plan_git.integration_branch(name)


def exception_of(exception: str, reason: str) -> tuple[str, str]:
    """Valide la nature et le motif d'une exception, sans tête.

    Sert à la CI : avant le plan, on ne connaît pas encore la tête que la
    demande va tester (le gel peut manquer, `--sha` peut être absent) ; la
    nature et le motif se vérifient là, la tête exacte est vérifiée et
    journalisée après le plan (`declare`, qui exige le 40 hex)."""
    kind = (exception or "").strip().lower()
    if kind not in EXCEPTIONS:
        raise PacketGuardError("exception: %r (one of %s)"
                               % (exception, ", ".join(EXCEPTIONS)))
    text = " ".join(str(reason or "").split())
    if not text:
        raise PacketGuardError("exception %s requires a reason: the exception is marked "
                               "(canon §11 bis)" % kind)
    return kind, text


def marker(exception: str, sha: str, reason: str) -> str:
    """La note-journal d'une exception (`packet-exception …`)."""
    kind, reason = exception_of(exception, reason)
    digest = (sha or "").strip().lower()
    if not _SHA.fullmatch(digest):
        raise PacketGuardError("exception %s: the full 40-hex head is required, not %r"
                               % (kind, sha))
    return "%s %s sha=%s reason=%s" % (MARKER, kind, digest, reason)


def _parse(note: str) -> dict | None:
    found = _DECLARATION.match((note or "").strip())
    if not found:
        return None
    return {"exception": found.group("exception"), "sha": found.group("sha"),
            "reason": found.group("reason") or "", "note": (note or "").strip()}


def declarations(db, lot) -> list[dict]:
    """Les exceptions déclarées au journal du lot, de la plus récente à la
    plus ancienne. Un journal illisible ne prouve aucune exception : la
    garde refuse."""
    if db is None or lot is None:
        return []
    from . import work
    try:
        events = work.events(db, int(lot), limit=500)
    except Exception:  # noqa: BLE001 - un journal illisible ne prouve rien
        return []
    out = []
    for event in events or ():
        parsed = _parse(event.get("note") or "")
        if parsed is None:
            continue
        parsed["id"] = int(event.get("id") or 0)
        out.append(parsed)
    return sorted(out, key=lambda row: row["id"], reverse=True)


def declared(db, lot, sha) -> dict | None:
    """L'exception déclarée pour cette tête exacte, ou None. Une exception
    d'une autre tête ne couvre jamais un nouveau gel."""
    wanted = (sha or "").strip().lower()
    if not _SHA.fullmatch(wanted):
        return None
    for row in declarations(db, lot):
        if row["sha"] == wanted:
            return row
    return None


def declared_in(events, sha) -> dict | None:
    """#2919 : l'exception déclarée pour cette tête exacte, lue dans un journal
    déjà chargé (la passe SLO lit celui de tous les lots ouverts en une fois).

    Même règle que `declared` : une exception d'une autre tête ne couvre
    jamais un nouveau gel ; la dernière déclaration de cette tête l'emporte."""
    wanted = (sha or "").strip().lower()
    if not _SHA.fullmatch(wanted):
        return None
    found = None
    for event in events or ():
        parsed = _parse(event.get("note") or "")
        if parsed is not None and parsed["sha"] == wanted:
            parsed["id"] = int(event.get("id") or 0)
            found = parsed
    return found


def declare(db, lot, exception: str, reason: str, sha: str, *, actor: str = "") -> dict:
    """Écrit la déclaration d'exception au journal du lot et la rend.

    Le geste est journalisé avant la fusion ou la CI : la preuve reste même
    si le geste échoue ensuite. Sans base (`db` None, chemin de test ou
    d'appel direct), la déclaration est validée mais pas écrite ; la CLI,
    elle, a toujours la base."""
    text = marker(exception, sha, reason)
    journaled = False
    if db is not None and lot is not None:
        from . import work
        work.note(db, int(lot), text, actor=actor or "")
        journaled = True
    return {"exception": (exception or "").strip().lower(),
            "sha": (sha or "").strip().lower(),
            "reason": " ".join(str(reason or "").split()), "note": text,
            "journaled": journaled}


def _merge_refusal(source: str, target: str) -> str:
    return (
        "direct merge of %s into %s refused: canon §11 bis (manaty/home#107, owner "
        "18/10/2026 « tout passe par paquet ») — no lot branch (anything but "
        "integration/**: lot/**, agent/**, review/**, fix/**…) is merged directly on the integration target; it enters by the "
        "packet (integration/**). If this lot fixes an incident (target red) or "
        "unblocks the CI, declare the exception in the lot's note: --exception "
        "incident|unblock --reason …"
        % (branch_of(source) or "?", branch_of(target) or "the integration target"))


def _ci_refusal(source: str, target: str) -> str:
    return (
        "ci-lot request for %s refused: canon §11 bis (manaty/home#107, owner "
        "18/10/2026 « tout passe par paquet ») — no lot CI (label ci-lot-<sha>) is "
        "started between packets; the packet runs one full suite (--full --reason …). "
        "If this lot unblocks the CI or fixes an incident (target red), declare the "
        "exception in the lot's note: --exception incident|unblock --reason …"
        % (branch_of(source) or "this lot"))


def check_merge(db, lot, source, target, sha, *, exception: str = "", reason: str = "",
                actor: str = "") -> dict:
    """Refuse une fusion de lot directe hors paquet, sauf exception déclarée.

    Une tête `integration/**` passe (c'est le paquet). Une cible qui n'est pas
    une cible d'intégration passe aussi : un lot empilé sur une autre branche
    de travail ne déplace pas `main`/`develop`. La cible vide est traitée
    comme une cible d'intégration (le doute refuse). Une exception explicite
    est journalisée ici, sur la tête `sha` ; une exception déjà déclarée est
    relue. Rend la déclaration retenue, ou lève `PacketGuardError`."""
    source_name, target_name = branch_of(source), branch_of(target)
    if not is_work_branch(source_name):
        return {"allowed": True, "why": "source %s is a packet or an integration ref"
                % source_name,
                "exception": "", "journaled": False}
    if not is_integration_target(target_name):
        return {"allowed": True, "why": "target %s is not an integration target"
                % target_name, "exception": "", "journaled": False}
    if exception:
        return {"allowed": True, **declare(db, lot, exception, reason, sha, actor=actor)}
    found = declared(db, lot, sha)
    if found is not None:
        return {"allowed": True, "exception": found["exception"],
                "reason": found["reason"], "declared": True, "journaled": True}
    raise PacketGuardError(_merge_refusal(source_name, target_name))


def check_ci(db, lot, source, *, mode: str = "lot", sha: str = "",
             exception: str = "", reason: str = "") -> dict:
    """Refuse une demande de CI de lot (`ci-lot`) hors paquet.

    Seul `mode == "lot"` est concerné : `--full` est la suite du paquet ou
    d'une release. Une exception explicite est acceptée : sa nature et son
    motif sont validés ici, sa tête exacte est vérifiée et journalisée après
    le plan, avec la tête que `cmd_ci_run` a retenue (elle peut manquer avant
    le plan : ni gel ni `--sha`, la tête distante est lue par `gel_plan`).
    Une exception déjà déclarée pour `sha` est relue. Rend la déclaration
    retenue, ou lève `PacketGuardError`."""
    if (mode or "").strip() != "lot":
        return {"allowed": True, "why": "full suite, not a ci-lot request",
                "exception": "", "journaled": False}
    if exception:
        # validation seule : la note est écrite par l'appelant avec la tête
        # que le plan vient de retenir (elle peut différer du gel, ou n'être
        # connue qu'après la lecture de la branche distante).
        kind, text = exception_of(exception, reason)
        return {"allowed": True, "exception": kind, "reason": text,
                "journaled": False, "pending": True}
    found = declared(db, lot, sha)
    if found is not None:
        return {"allowed": True, "exception": found["exception"],
                "reason": found["reason"], "declared": True, "journaled": True}
    raise PacketGuardError(_ci_refusal(source, "the integration target"))
