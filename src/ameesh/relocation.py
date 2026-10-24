# SPDX-License-Identifier: AGPL-3.0-only
"""Déplacement d'un agent entre deux hôtes ADMIS (lot L31, décision 0028).

Entre deux tours seulement : quand l'hôte courant est **sous pression** et
qu'un autre hôte admis pour l'agent est **disponible** (dernier relevé sans
franchissement), l'agent passe là-bas. Le bail est rendu par l'exécuteur
courant (`relocate` change l'hôte d'exécution, ce qui le rend non réclamable
sur l'ancien hôte) puis repris par l'exécuteur de l'hôte d'arrivée.

La session native n'est conservée que si le stockage des sessions est partagé
entre les hôtes ; sinon l'agent est déplacé avec un résumé de reprise en tête
de sa consigne (rotation, 0025). Aucun déplacement au milieu d'un tour, et les
comptes/identifiants (L30) sont revérifiés sur l'hôte d'arrivée par son propre
`canon sync`.

L159f : l'hôte d'arrivée doit aussi accepter des agents selon sa porte
d'hôte (`gate_open`) : jamais vers un hôte qui se retire.

#3299 : `ameesh agent move` déplace À LA MAIN un agent `releve: hote` vers un
autre hôte admis, quand son exécuteur de départ est arrêté (poste éteint
exprès, unité stoppée). Mêmes gardes que `relocate` — destination admise,
aucun tour, aucun handoff ouvert, aucun `host_hold` — sans exiger un bail
vivant quand l'exécuteur source ne sert plus l'agent (`runner_seen_at`
périmé, ou unité systemd inactive constatée sur place). L'appel écrit un
`agent.moved` dans le journal de l'agent, dans la transaction du déplacement.
"""
from __future__ import annotations

import subprocess
import time

from . import resources, storage
from .config import NAME_RE

#: message de reprise quand la session native n'est pas portable entre hôtes
FALLBACK_SUMMARY = (
    "Resuming after a move from host %s to %s: the previous session "
    "is not resumed (session storage is local to each host). Re-read your "
    "project thread and the state of your lot before continuing."
)

#: #3299 : type d'événement écrit dans le journal de l'agent (ameesh_events)
MANUAL_EVENT = "agent.moved"

#: #3299 : unité systemd de l'exécuteur d'un agent (deploy/systemd/…@.service)
RUNNER_UNIT = "ameesh-runner-agent@%s.service"


class MoveError(ValueError):
    """Déplacement manuel refusé — le message dit pourquoi et quoi faire."""


def runner_liveness_s(cfg) -> float:
    """#3299 : âge au-delà duquel `runner_seen_at` ne prouve plus un exécuteur
    vivant. Même fenêtre que #2625 (`reprise`) : deux passages de relevé, au
    moins deux minutes — un exécuteur arrêté cesse d'écrire, un exécuteur
    vivant écrit à chaque `runner_liveness_interval`."""
    poll = float(getattr(cfg, "poll", 5.0) or 5.0)
    interval = float(getattr(cfg, "runner_liveness_interval", 60.0) or 60.0)
    return 2.0 * max(60.0, poll, interval)


def unit_stopped(cfg, name: str, host: str) -> bool | None:
    """#3299 : l'unité de l'exécuteur de `name` est-elle inactive ICI ?

    Vrai seulement si `host` est l'hôte de cette commande, que l'unité est
    connue de systemd et qu'elle est `inactive`/`failed` ; Faux si elle est
    `active` ; None quand rien ne le prouve (autre hôte, systemd absent, unité
    inconnue ou transitionnelle). Une unité inconnue n'est pas une preuve : le
    repli est la péremption de `runner_seen_at`, en base."""
    if not host or host != getattr(cfg, "host", None):
        return None
    if not NAME_RE.match(name or ""):
        return None
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", RUNNER_UNIT % name,
             "--property=LoadState,ActiveState", "--no-pager"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    state: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            state[key.strip()] = value.strip()
    if state.get("LoadState") != "loaded":
        return None
    if state.get("ActiveState") in ("inactive", "failed"):
        return True
    if state.get("ActiveState") == "active":
        return False
    return None


def _manual_refusal(cfg, db, name: str, source: str, target: str) -> str:
    """Pourquoi l'écriture de déplacement manuel n'a rien changé (message)."""
    from . import host_holds, registry

    row = registry.get(db, name) or {}
    current = row.get("host") or ""
    if current and current != source:
        return ("%s left %s meanwhile (now on %s): nothing moved"
                % (name, source, current))
    if registry.turn_in_progress(db, name):
        return "%s has a turn in progress: never moved mid-turn" % name
    if row.get("status") == "running":
        return ("%s still has a stale 'running' status (no live lease): let the "
                "runner's reap settle it or run \"ameesh resume %s\", then retry"
                % (name, name))
    if target not in (row.get("admitted_hosts") or ()):
        return ("%s is no longer admitted on %s: its Placement changed meanwhile"
                % (name, target))
    if target in host_holds.live(db):
        return "host %s is on hold: no agent is moved onto it" % target
    if storage.of(db).handoffs.active(name):
        return ("%s has an open host handoff: finish or cancel it before moving it"
                % name)
    return ("%s was not moved: its source executor on %s still holds a live lease "
            "and reported less than %.0f s ago; stop that executor (or wait for "
            "runner_seen_at to go stale) and retry"
            % (name, source, runner_liveness_s(cfg)))


def move_manual(cfg, db, name: str, target: str, *, keep_session: bool = False,
                actor: str = "") -> dict:
    """#3299 : déplace `name` vers `target` à la main, sans bail de l'appelant.

    L'appelant (un opérateur) ne détient pas le bail de l'agent : la preuve que
    l'exécuteur de départ est arrêté vient de `runner_seen_at` (horloge de la
    base, périmée au-delà de `runner_liveness_s(cfg)`) ou de l'unité systemd
    inactive quand la commande tourne sur l'hôte source. Le déplacement et son
    journal (`agent.moved`) sont une seule transaction ; les gardes sont
    revérifiées sous le verrou par `storage.canon.relocate_operator`, qui
    révoque aussi le bail de la source (owner/échéance NULL, époque +1) :
    l'ancien exécuteur ne peut plus renouveler ni ouvrir un tour, même avec
    `keep_session` (B1 #3299).

    Rend `{agent, from, to, keep_session, summary, event, source_stopped}` ;
    lève `MoveError` (message actionnable) si le déplacement est refusé."""
    from . import agent_names, host_holds, registry

    name = agent_names.resolve(name, db=db, operation="relocation.move_manual")
    if not target or not NAME_RE.match(target):
        raise MoveError("invalid destination host: %r" % (target,))
    row = registry.get(db, name)
    if row is None:
        raise MoveError("unknown agent: %s" % name)
    source = row.get("host") or ""
    if not source:
        raise MoveError("%s has no execution host yet: run canon sync first" % name)
    if target == source:
        raise MoveError("%s is already on %s" % (name, target))
    if (row.get("releve") or "hote") == "election":
        raise MoveError(
            "%s is an elected role (releve: election): its candidates take the "
            "lease; a manual move would bypass the election" % name)
    admitted = list(row.get("admitted_hosts") or ())
    if target not in admitted:
        raise MoveError(
            "%s is not admitted on %s (admitted: %s): add the host to its "
            "Placement and refresh its admitted hosts with \"ameesh canon sync\" "
            "(the current host updates them, the arrival host evaluates placement)"
            % (name, target, ", ".join(admitted) or "nowhere"))
    if target in host_holds.live(db):
        raise MoveError("host %s is on hold: no agent is moved onto it" % target)
    if registry.turn_in_progress(db, name):
        raise MoveError("%s has a turn in progress: never moved mid-turn" % name)
    if row.get("status") == "running":
        raise MoveError(
            "%s still has a stale 'running' status (no live lease): let the "
            "runner's reap settle it or run \"ameesh resume %s\", then retry"
            % (name, name))
    if storage.of(db).handoffs.active(name):
        raise MoveError(
            "%s has an open host handoff: finish or cancel it before moving it"
            % name)
    stopped = unit_stopped(cfg, name, source)
    summary = "" if keep_session else FALLBACK_SUMMARY % (source, target)
    diagnostic = ("moved from %s by hand%s: placement to re-evaluate on %s"
                  % (source, " (%s)" % actor if actor else "", target))
    with db.transaction() as tx:
        moved = storage.of(tx).canon.relocate_operator(
            name, source, target=target, canon_ref=row.get("canon_ref") or "",
            diagnostic=diagnostic, ref=row.get("placement_ref"),
            keep_session=bool(keep_session), summary=summary,
            runner_stale_s=runner_liveness_s(cfg), source_stopped=stopped is True)
        if moved is None:
            raise MoveError(_manual_refusal(cfg, tx, name, source, target))
        # le bail révoqué est celui lu sous le verrou, pas celui de la lecture
        # préalable (une réclamation concurrente a pu le changer)
        previous_owner = moved.get("previous_lease_owner") or ""
        previous_epoch = int(moved.get("previous_lease_epoch") or 0)
        # preuve locale retenue par l'écriture (unité propre à l'agent), pas
        # l'état brut du gabarit systemd qui répond inactive pour tout nom
        proof = bool(moved.get("source_stopped"))
        data = {"agent": name, "from": source, "to": target,
                "keep_session": bool(keep_session), "actor": actor or "",
                "source_stopped": proof,
                "lease_held": bool(previous_owner),
                "revoked_lease_owner": previous_owner,
                "revoked_lease_epoch": previous_epoch,
                "lease_epoch": previous_epoch + 1,
                "runner_seen_ts": row.get("runner_seen_ts"),
                "runner_seen_host": row.get("runner_seen_host") or ""}
        event = storage.of(tx).events.record(
            MANUAL_EVENT, "agent:%s" % name, None, data)
    return {"agent": name, "from": source, "to": target,
            "keep_session": bool(keep_session), "summary": summary,
            "event": int(event), "source_stopped": proof,
            "revoked_lease_owner": previous_owner,
            "revoked_lease_epoch": previous_epoch}


def _score(reading: dict, offer: dict | None = None) -> tuple:
    """Classement d'un hôte disponible : le plus de mémoire, puis le moins de
    charge, puis le nom (stable).

    L159d: a lending host (an offer `policy.lend` in its Host fiche) ranks by
    its lendable memory, capped by the offer, and not by its raw free memory:
    what its application leaves free is not all ours to take."""
    mem = reading.get("mem_available_bytes")
    if offer is not None:
        lent = resources.offered(reading, offer)
        mem = lent["memory_bytes"] if lent is not None else None
    load = reading.get("load1")
    return (-(mem if mem is not None else -1),
            load if load is not None else float("inf"))


#: L159f : âge maximal du relevé qui porte la porte d'hôte (5 relevés au pas
#: par défaut de 60 s) : au-delà, l'exécuteur de l'hôte ne relève plus rien
#: (arrêté, hôte tombé) et son dernier `available` ne vaut plus
GATE_MAX_AGE_S = 300.0


def gate_open(reading: dict, offer: dict | None = None, *, now: float | None = None,
              max_age_s: float = GATE_MAX_AGE_S) -> bool:
    """L159f : la porte d'hôte du dernier relevé admet-elle des agents ?
    `available` dans un relevé frais, ou pas de porte sur un hôte classique.
    Un hôte prêteur (`offer`, son `policy.lend`) sans porte relevée est fermé
    par défaut ; une porte relevée trop ancienne (`max_age_s`) aussi."""
    state = reading.get("gate_state")
    if state is None:
        return offer is None
    if state != "available":
        return False
    sampled = reading.get("sampled_ts")
    now = time.time() if now is None else now
    return sampled is not None and now - float(sampled) <= max_age_s


def candidates(admitted_hosts, current_host: str, *, db,
               limits_for=None) -> list[dict]:
    """Hôtes admis autres que l'actuel, avec leur dernier relevé et pression.

    `limits_for(host)` rend les seuils de l'hôte (à défaut, les valeurs par
    défaut prudentes). #254 : jamais un hôte en pause."""
    from . import host_holds
    held = host_holds.live(db) if admitted_hosts else {}
    out: list[dict] = []
    for host in admitted_hosts or []:
        if not host or host == current_host or host in held:
            continue
        reading = storage.of(db).hosts.latest(host)
        if reading is None:
            continue
        limits = limits_for(host) if limits_for is not None else None
        verdict = resources.pressure(reading, limits=limits)
        out.append({"host": host, "reading": reading, "pressure": verdict})
    return out


def plan(admitted_hosts, current_host: str, *, db, current_limits=None,
         limits_for=None, shared_sessions: bool = False,
         gate_max_age_s: float = GATE_MAX_AGE_S) -> dict | None:
    """Plan de déplacement, ou None : l'hôte courant doit être sous pression et
    un hôte admis disponible doit exister (et être moins chargé)."""
    current_reading = storage.of(db).hosts.latest(current_host)
    if current_reading is None:
        return None
    current = resources.pressure(current_reading, limits=current_limits)
    if not current["blocked"]:
        return None
    dispo = [c for c in candidates(admitted_hosts, current_host, db=db, limits_for=limits_for)
             if not c["pressure"]["blocked"] and gate_open(
                 c["reading"], (c["pressure"].get("limits") or {}).get("lend"),
                 max_age_s=gate_max_age_s)]
    if not dispo:
        return None
    meilleur = min(dispo, key=lambda c: _score(
        c["reading"], ((c["pressure"].get("limits") or {}).get("lend"))) + (c["host"],))
    return {
        "target": meilleur["host"],
        "keep_session": bool(shared_sessions),
        "reason": "host %s under pressure (%s); %s available" % (
            current_host, ", ".join(b["key"] for b in current["breaches"]),
            meilleur["host"]),
        "current": current,
        "target_pressure": meilleur["pressure"],
    }


def move(db, row: dict, plan_dict: dict, *, current_host: str,
         owner: str, epoch: int) -> dict | None:
    """Applique le plan : change l'hôte d'exécution et la session de l'agent.

    `row` est la ligne du registre (nom, `canon_ref`, `placement_ref`),
    `admitted_hosts` déjà recopié par `canon sync`. `owner`/`epoch` sont le
    bail du worker : l'opération de stockage refuse une écriture d'un worker
    périmé, un tour en cours, ou une destination qui n'est plus admise."""
    name = row.get("name")
    if not name:
        return None
    keep = bool(plan_dict.get("keep_session"))
    summary = ""
    if not keep:
        summary = FALLBACK_SUMMARY % (current_host, plan_dict["target"])
    moved = storage.of(db).canon.relocate(
        name, current_host, target=plan_dict["target"],
        canon_ref=row.get("canon_ref") or "",
        diagnostic="moved from %s: placement to re-evaluate on %s (host pressure)"
                   % (current_host, plan_dict["target"]),
        ref=row.get("placement_ref"),
        keep_session=keep, owner=owner, epoch=epoch, summary=summary)
    if moved is None:
        return None
    return {"target": moved["host"], "keep_session": keep, "summary": summary,
            "reason": plan_dict["reason"]}
