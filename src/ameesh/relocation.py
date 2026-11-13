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
import uuid

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

#: #7108 : refus stable d'un déplacement d'avatar vers un tenant sans profil
#: de compte pour le harnais de l'agent (conception 5322 §3.5)
ACCOUNT_MISSING = "move-account-missing"


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
    handoff = storage.of(db).handoffs.active(name)
    if handoff:
        return open_handoff_refusal(name, handoff)
    return ("%s was not moved: its source executor on %s still holds a live lease "
            "and reported less than %.0f s ago; stop that executor (or wait for "
            "runner_seen_at to go stale) and retry"
            % (name, source, runner_liveness_s(cfg)))


def open_handoff_refusal(name: str, handoff: dict) -> str:
    """#5864 : le refus nomme l'opération ouverte et la commande de sortie."""
    state = handoff.get("state") or "?"
    if state == "held":
        state = "held at %s" % (handoff.get("evidence") or {}).get("held_at", "?")
    return ("%s has an open host handoff %s (%s, %s -> %s): inspect it with "
            "\"ameesh handoff show %s\", then finish it or cancel it with "
            "\"ameesh handoff cancel %s\" before moving it"
            % (name, handoff.get("id"), state, handoff.get("source"),
               handoff.get("target"), handoff.get("id"), handoff.get("id")))


def _preference_tenant_refusal(db, row: dict, target: str) -> str:
    """#7108 : sur un hôte partagé, l'agent doit être un tenant lié de la cible."""
    from . import canon as canon_mod, tenancy

    policy = tenancy.host_policy(db, target)
    if not policy.get("shared"):
        return ""
    tenant = canon_mod.tenant_key(row.get("responsible"))
    if not tenant or tenant not in policy.get("tenants", ()):
        return ("move-tenant-missing: %s is not a declared tenant of shared host %s"
                " (declared: %s)"
                % (tenant or "undeclared", target,
                   ", ".join(policy.get("tenants") or ()) or "none"))
    bound = [b for b in storage.of(db).tenancy.bindings(target) if b["tenant"] == tenant]
    if not bound:
        return ("move-tenant-missing: tenant %s is not bound on shared host %s"
                " (run 'ameesh host tenant add' on %s, as the host supervisor)"
                % (tenant, target, target))
    return ""


def _preference_presence_refusal(db, row: dict, target: str) -> str:
    """#7108 : la cible doit être PRÉSENTE (relevé, porte, canon, runner).

    `candidate_state` recomputes the whole presence from the database — fresh
    resource reading, host lifecycle and gate, canon state, placement verdict
    and fresh standing — and adds the tenant runner's heartbeat on a shared
    host (§3.2 point 5). The target host's standing itself carries the local
    harness/account readiness: this is why an unsupported harness is named
    here too, before any effect."""
    from . import election

    state = election.candidate_state(db, row, target)
    if state["ready"]:
        return ""
    diagnostic = state.get("diagnostic") or "not present"
    low = diagnostic.lower()
    # Un compte qui manque ou un identifiant en échec n'est pas un harnais
    # absent : « harness login failing » contient le mot « harness » et doit
    # rester `move-account-missing` (le refus que le demandeur doit corriger).
    if any(word in low for word in ("no usable account", "no account profile",
                                    "login", "credential", "all accounts",
                                    "account ")):
        return "move-account-missing: %s: %s" % (target, diagnostic)
    if "harness" in low:
        return "move-harness-missing: %s: %s" % (target, diagnostic)
    if "tenant" in low:
        return "move-tenant-missing: %s: %s" % (target, diagnostic)
    return "move-presence: %s: %s" % (target, diagnostic)


def _preference_account_refusal(db, row: dict, target: str) -> str:
    """#7108 : le tenant de la cible publie-t-il un profil pour le harnais ?

    Le profil vit sous le home 0700 du tenant, que le superviseur ne lit pas :
    le runner du tenant publie des noms `harnais/compte` dans son battement
    (0576). L'absence de profil est refusée AVANT tout effet, alors que les
    identifiants eux-mêmes restent locaux à chaque hôte (aucun fichier ne
    traverse). Un hôte non partagé n'a pas de tenant : sa préparation reste
    celle de l'exécuteur d'arrivée (présence et `conversation_move`)."""
    from . import canon as canon_mod, tenancy

    policy = tenancy.host_policy(db, target)
    if not policy.get("shared"):
        return ""
    tenant = canon_mod.tenant_key(row.get("responsible"))
    presence = storage.of(db).tenancy.presence(target, tenant) if tenant else None
    profiles = list((presence or {}).get("profiles") or ())
    harness = row.get("harness") or ""
    if not any(profile.split("/", 1)[0] == harness for profile in profiles):
        return ("%s: tenant %s of shared host %s publishes no account profile for "
                "harness %s (published: %s)"
                % (ACCOUNT_MISSING, tenant or "?", target, harness or "?",
                   ", ".join(profiles) or "none"))
    return ""


def _preference_target_cwd(db, name: str, target: str) -> str:
    """Le dossier de travail de la cible, écrit par son `canon sync`."""
    rows = db.query(
        "SELECT cwd FROM agent_candidates WHERE name = %s AND host = %s",
        (name, target))
    cwd = rows[0].get("cwd") if rows else None
    if not cwd:
        raise MoveError(
            'move-folder-missing: %s has no work folder on %s yet: run "ameesh '
            'canon sync --host %s" on %s, then retry'
            % (name, target, target, target))
    return cwd


def _preference_refusal(db, row: dict, target: str, *, actor: str) -> None:
    """Toutes les gardes du déplacement d'un avatar, avant toute écriture.

    Ordre : tour, handoff ouvert, tentative de conversation ouverte, pause de
    l'hôte, tenant, présence, profil de compte. Aucune ne lit ni ne copie un
    fichier de session ou d'identifiant ; les erreurs sont actionnables."""
    from . import host_holds, registry

    name = row["name"]
    # Un `status = running` sans bail vivant n'est pas un tour : l'hôte est
    # peut-être mort. `conversation_moves.manual` le traite en `recovered` et
    # rejoue une fois la consigne interrompue (design §2.3) ; seul un tour
    # vivant interdit le déplacement.
    if registry.turn_in_progress(db, name):
        raise MoveError("%s has a turn in progress: never moved mid-turn" % name)
    handoff = storage.of(db).handoffs.active(name)
    if handoff:
        raise MoveError(open_handoff_refusal(name, handoff))
    attempt = storage.of(db).conversation_moves.open_for(name)
    if attempt:
        raise MoveError(
            "move-conversation-open: %s already has an open conversation move %s"
            " (%s -> %s, %s): finish it, or let it expire, before moving by hand"
            % (name, attempt.get("move_id"), attempt.get("source_host"),
               attempt.get("target_host"), attempt.get("state")))
    if target in host_holds.live(db):
        raise MoveError("host %s is on hold: no agent is moved onto it" % target)
    for guard in (_preference_tenant_refusal, _preference_presence_refusal,
                  _preference_account_refusal):
        reason = guard(db, row, target)
        if reason:
            raise MoveError(reason)
    if not actor:
        raise MoveError(
            "a preference move needs an actor (its event journals who moved it)")


def move_preference(cfg, db, name: str, target: str, *, actor: str = "",
                    keep_session: bool = False, row: dict | None = None) -> dict:
    """#7108 (avatar B4) : `ameesh agent move` d'un avatar `releve: preference`.

    Préflight sans effet (présence, tenant, profil de compte, harnais, aucun
    tour, aucun handoff ouvert, aucune pause d'hôte), puis UNE transaction :
    `agent.moved` et la tentative `conversation_moves` (raison `manual`, état
    `switched` — `recovered` si le bail source n'est plus vivant, le tour
    interrompu est alors rejoué une fois) sont écrits en même temps que la
    ligne de registre passe sur la cible, le bail source est révoqué (époque +
    1) et la session native reste provisoire. La cible réclame ; son premier
    tour reprend le segment bâti sur le journal. Aucune session ni aucun
    identifiant ne traverse : le poste garde les siens, le cloud garde les
    siens (§3.5)."""
    from . import conversation_move, registry

    row = row if row is not None else registry.get(db, name)
    if row is None:
        raise MoveError("unknown agent: %s" % name)
    if keep_session:
        raise MoveError(
            "%s is a conversation move (releve: preference): --keep-session is "
            "refused; the native session never crosses hosts and the target "
            "rehydrates the conversation from the journal" % name)
    source = row.get("host") or ""
    if not source:
        raise MoveError("%s has no execution host yet: run canon sync first" % name)
    if target == source:
        raise MoveError("%s is already on %s" % (name, target))
    if (row.get("mode") or "execute") != "execute":
        raise MoveError("%s is not an executor agent (mode: %s): it cannot move"
                        % (name, row.get("mode")))
    if target not in (row.get("admitted_hosts") or ()):
        raise MoveError(
            "%s is not admitted on %s (admitted: %s): add the host to its "
            'Placement and refresh its admitted hosts with "ameesh canon sync"'
            % (name, target, ", ".join(row.get("admitted_hosts") or ()) or "nowhere"))
    _preference_refusal(db, row, target, actor=actor)
    target_cwd = _preference_target_cwd(db, name, target)
    move_id = str(uuid.uuid4())
    diagnostic = "moved from %s to %s by operator%s: conversation follows" % (
        source, target, " (%s)" % actor if actor else "")
    with db.transaction() as tx:
        moved = conversation_move.manual(
            tx, name, target, target_cwd, actor=actor, source_host=source,
            move_id=move_id)
        if moved is None:
            raise MoveError(
                "%s was not moved: its state changed during the preflight (host, "
                "lease, turn, handoff, hold or target admission); retry" % name)
        previous_owner = moved.get("previous_lease_owner") or ""
        previous_epoch = int(moved.get("previous_lease_epoch") or 0)
        data = {"agent": name, "from": source, "to": target,
                "keep_session": True, "reason": "manual",
                "conversation_move": moved["move_id"],
                "move_state": moved.get("state") or "switched",
                "session_copied": False, "credential_copied": False,
                "actor": actor or "", "source_stopped": False,
                "lease_held": bool(previous_owner),
                "revoked_lease_owner": previous_owner,
                "revoked_lease_epoch": previous_epoch,
                "lease_epoch": previous_epoch + 1,
                "runner_seen_ts": row.get("runner_seen_ts"),
                "runner_seen_host": row.get("runner_seen_host") or ""}
        event = storage.of(tx).events.record(
            MANUAL_EVENT, "agent:%s" % name, None, data)
    return {"agent": name, "from": source, "to": target, "keep_session": True,
            "summary": "", "event": int(event), "source_stopped": False,
            "move_state": moved.get("state") or "switched",
            "revoked_lease_owner": previous_owner,
            "revoked_lease_epoch": previous_epoch,
            "conversation_move": moved["move_id"], "target_cwd": target_cwd,
            "diagnostic": diagnostic}


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
    releve = row.get("releve") or "hote"
    if releve == "election":
        raise MoveError(
            "%s is a cross-host role (releve: election): its election takes the "
            "lease; a manual move would bypass the relief" % name)
    if releve == "preference":
        # #7108 (avatar B4, conception 5322 §3.5) : déplacement immédiat d'un
        # avatar, même transaction `agent.moved` + `conversation_moves`.
        return move_preference(cfg, db, name, target, actor=actor,
                               keep_session=keep_session, row=row)
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
    handoff = storage.of(db).handoffs.active(name)
    if handoff:
        raise MoveError(open_handoff_refusal(name, handoff))
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
