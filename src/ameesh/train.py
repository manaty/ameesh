# SPDX-License-Identifier: AGPL-3.0-only
"""Event-driven integration trains (#1467).

This module is deliberately not installed in the executor yet. Ports must
qualify historical authors/reviewers and use the pinned canon's promotion
authority before an enabled train can run. No lot note grants authority.

One call does bounded work, never waits for CI. The persisted state precedes
each external effect; a restart reconciles the same immutable plan. Every
recomposition (including a diagnostic half) gets its own complete CI.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Protocol

from . import merge_queue

SCHEMA = "ameesh-train/1"
TERMINAL = ("landed", "empty", "paused", "invalidated")
#: #7967: phases a dead packet reservation may terminalize without promotion.
#: `publishing` and `promoting` are excluded: a push or a merge may have left
#: the process, so only the pilot may conclude those (fail closed).
EXPIRABLE = ("ci-intent", "ci", "ready", "draining")


class TrainError(RuntimeError):
    """A missing proof or an uncertain effect: do not merge."""


class SuiteRefused(TrainError):
    """#7967 : la CI d'une tête a rendu un refus terminal.

    Le run est `completed` (le lecteur ne lève jamais ceci sur un run encore
    en cours ni sur une lecture impossible) mais sa suite requise manque, a
    été annulée, ne nomme pas le workflow appelé au head exact (#7971) ou ne
    prouve pas son résultat. Attendre un autre résultat retiendrait la cible
    pour rien : le plan est terminé sans promotion, et la coupure due
    recompose."""


class PrevalidationRed(TrainError):
    """La prévalidation de la composition a exécuté la sélection et un test a
    échoué : un résultat, pas une réponse perdue.

    L'Engine ne doit pas la confondre avec une publication incertaine (qui
    garde `publishing`, la réservation et rejoue la même composition à chaque
    événement) : le paquet est arrêté (`paused`, terminal) et la réservation
    est rendue par le `finally` de `step`."""


class PublicationRefused(TrainError):
    """#6978 : un refus local et déterministe avant tout effet distant (contrat
    CI audité absent ou différent à la tête du plan) : la même composition
    immuable serait refusée à l'identique à chaque passe.

    Ce n'est pas une réponse perdue : l'Engine invalide le plan (terminal,
    réservation rendue) au lieu de garder `publishing` jusqu'à la date limite.
    Une indisponibilité de prévalidation n'en est pas une (non bloquante)."""


class TrainObjectMissing(TrainError):
    """#8480 : un commit du plan (tête gelée, base) reste absent du clone de
    ce moteur après le fetch borné de la branche d'intégration et de la cible.

    Le clone est propre au propriétaire de la réservation : un plan composé
    et publié par un autre runner n'y existe pas tant qu'il n'est pas
    récupéré. Absent après ce fetch, c'est un incident nommé : le plan reste
    en `draining` (cible tenue, aucune promotion) et le pilote est prévenu une
    fois par commit manquant, jamais une boucle muette."""
    def __init__(self, plan_id, sha, why):
        super().__init__(why)
        self.incident = dict(kind="train-object-missing", plan=plan_id, sha=sha)


class MemberDeferred(TrainError):
    """A named composition exclusion, never an accusation or queue ejection."""
    def __init__(self, row, reason):
        super().__init__(reason)
        self.admission = identity(row)


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    # Build/beta uses the installed passage's compiled packet calendar.
    build_beta: bool = True
    interval_s: int = 3600
    threshold: int = 5
    # No implicit extra CI for the packet-replaces-lot-CI rule (canon §12).
    ci_mode: str = "packet"
    max_failures: int = 8
    # The installed passage supplies the compiled canon calendar (#1616).
    # None has no calendar: a cutoff is refused, never a local default (#2451).
    schedule: object | None = None

    def __post_init__(self):
        if self.ci_mode not in ("packet", "targeted"):
            raise ValueError("ci_mode: packet | targeted expected")
        if self.build_beta and self.ci_mode != "packet":
            raise ValueError("build/beta requires packet CI without per-lot CI")
        if self.interval_s <= 0 or self.threshold <= 0 or self.max_failures <= 0:
            raise ValueError("positive train limits expected")

    def cutoff(self, now):
        if self.schedule is None:
            raise TrainError("compiled canon calendar required for a packet cutoff")
        at = self.schedule.last_packet(now)
        if isinstance(at, bool) or not isinstance(at, (int, float)) \
                or not math.isfinite(at) or at > now:
            raise TrainError("compiled canon must provide a packet cutoff at or before now")
        return at


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def evidence(proof) -> dict:
    """The admission evidence, without the canon commit it was read at.

    The evidence is the review requirement of each member: class, count,
    classified paths, named rules and the historical verdicts. The policy
    commit is provenance, not evidence: a canon pin may move while a packet
    is in flight, and a move that leaves every requirement identical — for
    instance a `packets.cutoffs`-only change — must not throw that packet
    away (#8067). Any change that touches a member's requirement still
    changes its proof and refuses the plan.
    """
    trimmed = copy.deepcopy(proof)
    members = trimmed.get("members") if isinstance(trimmed, dict) else None
    for member in members or ():
        policy = member.get("policy") if isinstance(member, dict) else None
        if isinstance(policy, dict):
            policy.pop("policy_commit", None)
    return trimmed


def identity(row: dict) -> tuple:
    """Requeueing the same SHA must still invalidate an old admission."""
    return tuple(row[k] for k in ("work_item_id", "generation", "target", "frozen_id",
                                  "frozen_sha", "source"))


def ordered(rows: list[dict], dependencies: list[dict]) -> list[dict]:
    """Stable queue order, with prerequisites first; blocked/cyclic lots wait.

    `dependencies` is the unbounded storage read, including finished edges.
    An absent open prerequisite is never assumed to be merged.
    """
    remaining = sorted(rows, key=lambda r: (r.get("priority") or 2,
                                            r.get("rank") or math.inf,
                                            r.get("queued_ts") or 0,
                                            r["work_item_id"]))
    needs: dict = {}
    for edge in dependencies:
        if edge["blocking"]:
            needs.setdefault(int(edge["work_item_id"]), set()).add(int(edge["blocker_id"]))
    done, result = set(), []
    while remaining:
        ready = next((r for r in remaining if needs.get(int(r["work_item_id"]), set()) <= done),
                     None)
        if ready is None:
            break
        result.append(ready)
        done.add(int(ready["work_item_id"]))
        remaining.remove(ready)
    return result


def _admitted(ports, rows: list[dict], cutoff: float) -> list[dict]:
    """Rows admissible at `cutoff`, through the port or its queue fallback."""
    admitted = getattr(ports, "admitted", None)
    if callable(admitted):
        return admitted(rows, cutoff)
    return [r for r in rows if float(r["queued_ts"]) <= cutoff]


def due(rows: list[dict], now: float, policy: Policy, *, last_started: float = 0,
        recompose: bool = False, chain: bool = False) -> bool:
    if not rows:
        return False
    if policy.build_beta:
        # `rows` is already the admissible set for the cutoff: the engine
        # filters by the candidate's admissibility instant, not by the queue
        # insertion its own enrollment wrote after the cutoff (#2965). `due`
        # must not re-apply the queue timestamp, or that rescued row would
        # still skip the packet.
        slot = policy.cutoff(now)
        if recompose:
            # #8096 : un plan paused invalidé (ou abandonné par le pilote)
            # est un créneau consommé sans promotion. Le remplacement est dû
            # tout de suite, sans attendre le créneau suivant (§11 bis).
            return True
        if chain:
            # #8304 : le paquet suivant part dès que la fusion du précédent
            # est vue, si un lot est prêt (décision owner #154026 a, §11 bis).
            # Le créneau compilé reste le rythme quand aucun lot n'est prêt
            # dans l'heure ; il n'est jamais une borne devant un lot déjà prêt.
            return True
        return last_started < slot and bool(rows)
    if recompose or chain:
        return True
    # First train: at most an hour from the oldest admitted lot. Subsequent
    # trains: at most an hour since the preceding train, never an idle burst.
    since = max(last_started, min(float(r["queued_ts"]) for r in rows))
    return len(rows) >= policy.threshold or now - since >= policy.interval_s


class Ports(Protocol):
    """Trusted adapters; implementations must fail closed on unreadable facts.

    `qualify` checks exact freeze, all historical author/reviewer families,
    unresolved B/P2-sensitive, delta reviews, protected/release targets, fire
    (including withdrawal) and targeted CI only in that policy mode. It
    returns JSON evidence, not a boolean inferred from the latest verdict.
    `admitted` (#2965, optional) filters the queue rows to those admissible at
    a packet cutoff: the rows the train enrolled itself are dated by the
    admissibility instant of their admission proof, every other admission by
    its own queue instant. `authorize` binds
    (source,target,base,head,tree,members,proof) to the compiled merge policy,
    or to a named pilot fire when that policy requires one. Named-fire
    withdrawal is serialized with `promote`. `delivered` (#7885) is a
    read-only lifecycle fact: every plan member already reached a terminal
    delivery state outside this train, so the plan can no longer produce an
    effect and must not keep the reservation waiting for a CI that can never
    be promoted.
    """
    def acquire(self, epoch: int | None) -> int | None: ...
    def release(self, epoch: int) -> None: ...
    def load(self) -> dict | None: ...
    def save(self, state: dict, expected: int, epoch: int) -> dict: ...
    def target(self) -> str: ...
    def entries(self) -> list[dict]: ...
    def admitted(self, rows: list[dict], slot: float) -> list[dict]: ...
    def dependencies(self, rows: list[dict]) -> list[dict]: ...
    def qualify(self, rows: list[dict], base: str, mode: str) -> dict: ...
    def sync(self, base: str) -> dict | None: ...
    def compose(self, base: str, rows: list[dict], nonce: str,
                sync: dict | None = None) -> dict: ...
    def validate_transports(self, plan: dict) -> list[dict]: ...
    def publish(self, plan: dict, epoch: int) -> None: ...
    def published(self, plan: dict) -> str: ...
    def delivered(self, plan: dict) -> bool: ...
    def start_ci(self, plan: dict) -> None: ...
    def ci(self, plan: dict) -> dict: ...
    def authorize(self, plan: dict) -> dict: ...
    def promote(self, plan: dict, fire: dict, epoch: int) -> None: ...
    def reconcile(self, plan: dict, fire: dict, epoch: int) -> dict: ...
    def quarantine(self, rows: list[dict], reason: str, epoch: int) -> None: ...
    def notify(self, reason: str, state: dict) -> None: ...


class Engine:
    def __init__(self, ports: Ports, *, source: str, target: str, policy: Policy):
        self.ports, self.source, self.target, self.policy = ports, source, target, policy
        self.epoch: int | None = None

    def _save(self, state: dict) -> dict:
        return self.ports.save(state, int(state.get("revision", 0)), self.epoch)

    def _held(self):
        if self.ports.acquire(self.epoch) != self.epoch:
            raise TrainError("target reservation lost")

    def _plan(self, state, members, now, *, diagnostic=False, suspects=None,
              preflight=True):
        proof = self.ports.qualify(members, state["base"], self.policy.ci_mode)
        if not isinstance(proof, dict) or not proof:
            raise TrainError("missing structured admission evidence")
        number = len(state["attempts"]) + 1
        nonce = digest([self.source, self.target, state["base"], state["started_ts"],
                        [identity(r) for r in members], proof, number])
        sync = self.ports.sync(state["base"])
        merged = (self.ports.compose(state["base"], members, nonce, sync=sync)
                  if sync is not None
                  else self.ports.compose(state["base"], members, nonce))
        # The adapter refuses conflicts unless their exact merged delta is
        # reviewed. No implicit rerere approval and no inferred CI transfer.
        for field in ("head", "tree"):
            value = merged.get(field, "")
            if not merge_queue._HEX.fullmatch(value) or len(value) not in (40, 64):
                raise TrainError("compose returned no full %s" % field)
        token = digest([self.source, self.target, state["base"],
                        [identity(r) for r in members], merged, proof, number])
        plan = dict(schema=SCHEMA, source=self.source, target=self.target, base=state["base"],
                    members=copy.deepcopy(members), proof=proof, head=merged["head"],
                    tree=merged["tree"], diagnostic=diagnostic,
                    suspects=copy.deepcopy(suspects if suspects is not None else members),
                    nonce=nonce,
                    branch="integration/train-%s-%s" % (state["stamp"], token[:16]),
                    id=token, created_ts=now)
        if sync is not None:
            receipt = merged.get("sync")
            if not isinstance(receipt, dict) or receipt.get("ref") != sync.get("ref") \
                    or receipt.get("sha") != sync.get("sha") \
                    or receipt.get("base") != state["base"]:
                raise TrainError("compose returned no exact main sync receipt")
            for field in ("head", "tree"):
                value = receipt.get(field, "")
                if not merge_queue._HEX.fullmatch(value) or len(value) not in (40, 64):
                    raise TrainError("compose returned no full main sync %s" % field)
            plan["sync"] = copy.deepcopy(receipt)
        if "transports" in merged:
            if not isinstance(merged["transports"], list) or not merged["transports"]:
                raise TrainError("nonempty structured composition transports required")
            plan["transports"] = copy.deepcopy(merged["transports"])
        if "migrations" in merged:
            # #2400: the persisted mapping is what makes a reconstruction use
            # the same numbers instead of allocating again.
            entries = merged["migrations"]
            if not isinstance(entries, list) or not entries:
                raise TrainError("nonempty structured composition migrations required")
            known = {r["work_item_id"] for r in members}
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("work_item_id") not in known:
                    raise TrainError("composition migration mapping names an unknown member")
                if not all(isinstance(entry.get(field), int)
                           for field in ("reserved", "landed")):
                    raise TrainError("composition migration mapping needs reserved and "
                                     "landed numbers")
            plan["migrations"] = copy.deepcopy(entries)
        if preflight and not diagnostic:
            # #8305 : une source CI auditée n'est pas un conflit de fusion.
            # Le port nomme le SEUL lot dont le delta propre a introduit la
            # différence à la tête composée ; l'Engine le retire comme un
            # MemberDeferred et recompose les autres. Un arbre final conforme
            # est accepté avant cette attribution ; un héritage de base ou une
            # lecture Git impossible ne nomme personne (la porte de
            # publication, inchangée, tranche).
            hook = getattr(self.ports, "preflight", None)
            if callable(hook):
                hook(plan)
        state["plan"] = plan
        state["phase"] = "publishing"
        state["attempts"].append(dict(id=token, head=plan["head"], tree=plan["tree"],
                                      members=[r["work_item_id"] for r in members],
                                      diagnostic=diagnostic))
        return self._save(state)

    def _current(self, state):
        plan = state["plan"]
        if self.ports.target() != plan["base"]:
            raise TrainError("target changed during reserved CI: recompose and qualify again")
        current = {r["work_item_id"]: r for r in self.ports.entries()}
        for row in plan["members"]:
            other = current.get(row["work_item_id"])
            if other is None or identity(other) != identity(row):
                raise TrainError("member withdrawn, refrozen or requeued")
        proof = self.ports.qualify(plan["members"], plan["base"], self.policy.ci_mode)
        if digest(evidence(proof)) != digest(evidence(plan["proof"])):
            raise TrainError("admission evidence changed: recompose and qualify again")
        if plan.get("transports"):
            validate = getattr(self.ports, "validate_transports", None)
            if not callable(validate) or digest(validate(plan)) != digest(plan["transports"]):
                raise TrainError("qualified transport evidence changed before train effect")

    def _ci(self, state):
        ci = self.ports.ci(state["plan"])
        if ci.get("head") != state["plan"]["head"] \
                or ci.get("tree") != state["plan"]["tree"]:
            raise TrainError("CI evidence is for another commit or tree")
        if ci.get("status") not in ("pending", "absent", "green", "red"):
            raise TrainError("unknown CI outcome")
        if ci["status"] in ("green", "red") and not ci.get("complete"):
            raise TrainError("CI is not the complete required suite")
        return ci

    def step(self, now: float) -> dict:
        """One executor/event pass. No loops waiting for CI or a verdict."""
        if not self.policy.enabled:
            return {"result": "off"}
        epoch = self.ports.acquire(self.epoch)
        if epoch is None:
            self.epoch = None
            return {"result": "busy"}
        self.epoch = epoch
        state = self.ports.load()
        try:
            if state is not None and (state["source"], state["target"]) != (self.source, self.target):
                raise TrainError("persisted train belongs to another repository or target")
            if state is not None and state.get("abandon_request"):
                abandoned = self._requested_abandon(state, now)
                if abandoned is not None:
                    state = abandoned.pop("state")
                    return abandoned
            if state is not None and state["phase"] == "paused":
                # #8096 : un plan en pause dont un membre nommé a quitté
                # `validated` (revenu en build, retiré ou regelé) ne peut plus
                # être promu et n'a plus de raison d'attendre un abandon
                # manuel du pilote. Le service l'invalide sous son bail, avec
                # la trace du membre, et la passe suivante recompose sans lui.
                # Sans lecture complète des membres, on reste en pause.
                left = self._paused_members_left(state)
                if left is not None:
                    return self._paused_invalidated(state, left, now)
                return {"result": "paused"}
            if state is not None and state.get("phase") in EXPIRABLE:
                # #7967 : l'échéance absolue du ticket de réservation est
                # passée : le plan ne peut plus être promu sous ce bail et le
                # ticket mort n'est plus renouvelé. La cible, elle, reste
                # tenue par le verrou ordinaire que la maintenance renouvelle
                # jusqu'à l'issue terminale des runs. L'échéance seule ne
                # prouve pas cette fin : libérer la cible pendant qu'une CI
                # de paquet tourne lancerait la suivante par-dessus (§11 bis).
                # Seule une issue terminale établie termine le plan ; sinon il
                # reste en `draining`, cible tenue, et l'alerte `train_stuck`
                # (cadence du projet) nomme l'attente.
                # `publishing`/`promoting` restent au pilote (effet incertain).
                deadline = self._expired_reservation(state, now)
                if deadline is not None:
                    return self._expired_step(state, deadline)
            if state is not None and state["phase"] in (
                    "publishing", "ci-intent", "ci", "ready", "draining") \
                    and self._members_delivered(state):
                # #7885 (2) : chaque membre est déjà livré, fusionné ou clos
                # par une autre voie. Le plan ne peut plus rien produire : sa
                # CI (si elle existe) n'est pas une promotion possible et
                # l'attendre retiendrait la réservation pour toujours.
                return self._drained_terminal(
                    state, "all plan members are already delivered, merged or "
                    "closed outside the train: obsolete plan invalidated without "
                    "promotion")
            if state is not None and state["phase"] == "draining":
                # Withdrawal or stale evidence invalidates admission, not a
                # running CI. Keep the shared reservation until that exact
                # packet has a complete terminal outcome. Never promote it.
                ci = self._ci(state)
                if ci["status"] == "pending":
                    return {"result": "draining", "plan": state["plan"]}
                if ci["status"] == "absent" and not state.get("fire") \
                        and self._unpublished(state):
                    # #7885 (1) : ni feu ni publication, donc aucune CI n'a
                    # été ni ne sera lancée pour cette composition exacte.
                    # Attendre « absent » à jamais bloquait le train entier.
                    return self._drained_terminal(
                        state, "draining plan has no fire, no published train "
                        "reference and no CI run: no terminal outcome will ever "
                        "exist; plan invalidated without promotion")
                if ci["status"] == "absent":
                    # Une publication existe : la CI a pu partir et peut
                    # encore apparaître. L'alerte de durée (option du
                    # signalement) n'est pas retenue ici : elle relève du
                    # suivi du train, avec son propre seuil et destinataire.
                    return {"result": "draining", "plan": state["plan"]}
                state["attempts"][-1]["ci"] = ci
                state["phase"] = "invalidated"
                state = self._save(state)
                # End this passage before composing again: the caller must
                # recheck its canon fence for the next packet (#2914).
                return {"result": "invalidated", "plan": state["plan"]}
            if state is None or state["phase"] in TERMINAL:
                enroll = getattr(self.ports, "enroll", None)
                if callable(enroll):
                    enroll(epoch)
                    self._held()
                    # #3139: enrollment persists its per-lot refusals in the
                    # same document. Reload its exact revision before this
                    # pass writes anything (a stale revision would refuse).
                    state = self.ports.load()
                rows = self.ports.entries()
                recompose = self._recompose_pending(state)
                # #8304 : un paquet livré (ou invalidé avec une composition
                # devenue différente) rend le suivant dû sans attendre le
                # créneau compilé. Le marqueur #8096 reste prioritaire : il
                # prend les candidats courants sans filtre de créneau.
                chain = (self.policy.build_beta and not recompose
                         and self._chain_pending(state, now, rows))
                if chain:
                    rows = self._chain_candidates(state, rows)
                if self.policy.build_beta and not recompose:
                    # A late admission belongs to the next packet, even if
                    # this cutoff was empty or the executor has restarted.
                    # #2965: the instant is the candidate's admissibility,
                    # read by the admission reader — not the queue insertion
                    # this train itself just wrote after the cutoff. A manual
                    # admission keeps its own queue instant and stays late.
                    # #8304 : l'enchaînement est jugé sur les candidats
                    # admissibles à l'instant de la passe, jamais sur le
                    # créneau que le paquet précédent a déjà consommé.
                    slot = self.policy.cutoff(now)
                    rows = _admitted(self.ports, rows, now if chain else slot)
                # #8096 (B cx2 #45448) : quand un créneau a été consommé sans
                # promotion, la recomposition due prend les candidats
                # admissibles courants, y compris un nouveau gel qualifié
                # après ce créneau ; sinon le train resterait idle jusqu'au
                # suivant alors que le marqueur dit la recomposition due.
                rows = ordered(rows, self.ports.dependencies(rows))
                if not due(rows, now, self.policy,
                           last_started=float((state or {}).get("started_ts", 0)),
                           recompose=recompose, chain=chain):
                    return {"result": "idle"}
                from datetime import datetime, timezone
                state = dict(schema=SCHEMA, source=self.source, target=self.target,
                             revision=int((state or {}).get("revision", 0)), phase="composing",
                             base=self.ports.target(), members=rows, started_ts=now,
                             stamp=datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M"),
                             attempts=[], failures=0, pending=[], excluded=[], good=[], deferred=[],
                             enrollment_deferred=copy.deepcopy(
                                 (state or {}).get("enrollment_deferred") or []),
                             enrollment_checked_ts=(state or {}).get("enrollment_checked_ts"),
                             # #6978 : une demande nommait un plan précédent.
                             abandon_request=None)
                state = self._save(state)
            if self.policy.build_beta and (state.get("plan") or {}).get("diagnostic"):
                raise TrainError("diagnostic CI is disabled during build/beta")
            if state["phase"] == "composing":
                # Bounded by the number of admissions: no CI is dispatched
                # while incompatible contexts and their dependents are removed.
                while state["members"]:
                    try:
                        state = self._plan(state, state["members"], now)
                        break
                    except MemberDeferred as exc:
                        if exc.admission not in [identity(r) for r in state["members"]]:
                            raise TrainError("composition deferred an unknown admission") from exc
                        candidates = [r for r in state["members"] if identity(r) != exc.admission]
                        available = ordered(candidates, self.ports.dependencies(candidates))
                        kept = {identity(r) for r in available}
                        deferred = [r for r in state["members"] if identity(r) not in kept]
                        state["deferred"].extend(deferred)
                        state.setdefault("selection", []).append(dict(
                            admission=exc.admission, reason=str(exc),
                            deferred=[identity(r) for r in deferred]))
                        state["members"] = available
                        state = self._save(state)
                if state["phase"] == "composing":
                    state["phase"] = "empty"
                    state = self._save(state)
                    return {"result": "empty", "selection": state.get("selection", [])}
            if state["phase"] == "publishing":
                self._current(state)
                self._held()
                # Idempotent compare-and-swap publication of an immutable ref.
                try:
                    self.ports.publish(state["plan"], self.epoch)
                except PrevalidationRed as exc:
                    # Un rouge de prévalidation est un résultat, pas une
                    # réponse perdue : on arrête le paquet (terminal, donc
                    # réservation rendue) au lieu de rejouer la composition.
                    return self._prevalidation_red(state, exc)
                except PublicationRefused as exc:
                    # #6978 : refusé avant le bail distant, donc rien n'a été
                    # poussé dans cette passe. Une passe antérieure a pu
                    # pousser — réponse perdue (marqueur), ou mort du
                    # processus entre le push et la sauvegarde suivante, sans
                    # marqueur (B cx2 #213473) : la réservation n'est rendue
                    # que si la référence distante du plan est lue absente.
                    # Le marqueur reste un diagnostic, pas la décision : une
                    # erreur pré-effet (prévalidation, bail) le pose aussi
                    # (B qa-01 #39438).
                    if self._unpublished(state):
                        return self._publication_refused(state, exc)
                    self.ports.notify(str(exc), state)
                    return {"result": "publication-uncertain", "error": str(exc)}
                except TrainError as exc:
                    # A push may have succeeded despite a lost response.
                    # Keep the immutable publication intent for reconciliation.
                    if not state.get("publication_uncertain"):
                        state["publication_uncertain"] = True
                        state = self._save(state)
                    self.ports.notify(str(exc), state)
                    return {"result": "publication-uncertain", "error": str(exc)}
                state["phase"] = "ci-intent"
                state = self._save(state)
                self._held()
                # An uncertain dispatch is never repeated. A restart in
                # ci-intent reconciles runs on the same exact head below.
                try:
                    self.ports.start_ci(state["plan"])
                except TrainError as exc:
                    self.ports.notify(str(exc), state)
                    return {"result": "ci-uncertain", "error": str(exc)}
                state["phase"] = "ci"
                state = self._save(state)
                return {"result": "ci", "plan": state["plan"]}
            if state["phase"] in ("ci", "ci-intent"):
                self._current(state)
                ci = self._ci(state)
                if ci.get("status") in ("pending", "absent"):
                    return {"result": "ci", "plan": state["plan"]}
                state["attempts"][-1]["ci"] = ci
                if ci["status"] == "red":
                    return self._red(state, now)
                if state["plan"]["diagnostic"]:
                    state["good"].extend(state["plan"]["members"])
                    return self._next_probe(state, now)
                state["phase"] = "ready"
                state = self._save(state)
            elif state["phase"] == "ready":
                # A fire may arrive long after the initial green. A rerun or
                # a newer workflow attempt must supersede that old evidence.
                self._current(state)
                ci = self._ci(state)
                if ci["status"] in ("pending", "absent"):
                    state["phase"] = "ci"
                    self._save(state)
                    return {"result": "ci", "plan": state["plan"]}
                state["attempts"][-1]["ci"] = ci
                if ci["status"] == "red":
                    return self._red(state, now)
                state = self._save(state)
            if state["phase"] == "ready":
                fire = self.ports.authorize(state["plan"])
                if not fire:
                    return {"result": "awaiting-fire", "plan": state["plan"]}
                self._held()
                # Persist intent before the external effect. A lost response
                # is reconciled; it must never restart the pre-merge path.
                state["phase"] = "promoting"
                state["fire"] = copy.deepcopy(fire)
                state = self._save(state)
                self.ports.promote(state["plan"], fire, self.epoch)
                state["phase"] = "landed"
                # #8304 : l'instant de la fusion vue ouvre la fenêtre où le
                # paquet suivant est dû dès qu'un lot est prêt (§11 bis), et
                # la trace des candidats déjà prêts garde ce dû au-delà de
                # l'heure après un redémarrage (B cx2 #234583).
                state["landed_ts"] = now
                state["landed_ready"] = self._landing_ready(now, state["plan"])
                state = self._save(state)
                return {"result": "landed", "plan": state["plan"]}
            if state["phase"] == "promoting":
                if self.ports.target() == state["plan"]["head"]:
                    receipt = self.ports.reconcile(state["plan"], state["fire"], self.epoch)
                    if not isinstance(receipt, dict) or receipt.get("state") != "landed" \
                            or receipt.get("id") != state["fire"].get("id") \
                            or digest(receipt.get("plan")) != digest(state["plan"]):
                        raise TrainError("missing exact landed receipt after reconciliation")
                    state["phase"] = "landed"
                    state["landed_ts"] = now
                    state["landed_ready"] = self._landing_ready(now, state["plan"])
                    state = self._save(state)
                    return {"result": "landed", "plan": state["plan"]}
                # An uncertain push/merge is inspected by the pilot. Do not
                # construct another train or resend an external merge.
                raise TrainError("uncertain promotion: inspect the target and train proof")
            raise TrainError("unknown persisted phase %s" % state["phase"])
        except TrainError as exc:
            if state is not None:
                if isinstance(exc, SuiteRefused) and state.get("phase") in EXPIRABLE:
                    # #7967 : un run terminé qui ne prouve pas sa suite requise
                    # est un refus du contrat CI, pas une CI encore en cours.
                    # Aucun résultat différent ne peut plus arriver pour cette
                    # tête : terminer sans promotion (la coupure due repart)
                    # au lieu d'attendre l'échéance de la réservation.
                    return self._drained_terminal(state, str(exc), stage="ci")
                # Keeping a failed promotion's intent makes uncertainty
                # durable even if notifying the pilot fails.
                if state.get("phase") != "promoting":
                    state["phase"] = ("draining" if state.get("phase") in
                        ("publishing", "ci-intent", "ci", "ready", "draining")
                        else "invalidated")
                    state["error"] = str(exc)
                    state = self._save(state)
                self.ports.notify(str(exc), state)
            refused = {"result": "refused", "error": str(exc)}
            if isinstance(exc, TrainObjectMissing):
                refused["incident"] = dict(exc.incident)
            return refused
        finally:
            if state is None or state.get("phase") in TERMINAL:
                self.ports.release(epoch)
                self.epoch = None

    def _prevalidation_red(self, state, exc):
        """Arrête un paquet dont la prévalidation a exécuté et échoué.

        La composition est immuable : rester en `publishing` rejouerait le
        même rouge à chaque événement et tiendrait la réservation. `paused`
        est terminal — le `finally` rend l'époque — et le diagnostic porte les
        tests en échec. Comme après un CI rouge en build/beta, le train reste
        arrêté et attend le pilote : `step()` court-circuite `paused`, aucun
        créneau ne repart tout seul (même comportement que `_red`). Aucun
        diagnostic automatique n'est lancé : un échec multi-lots ne prouve pas
        un coupable.
        """
        state["failures"] += 1
        state["diagnosis"] = {"stage": "prevalidation", "why": str(exc)}
        state["phase"] = "paused"
        state = self._save(state)
        self.ports.notify("prevalidation red before publication: %s" % exc, state)
        return {"result": "paused", "error": str(exc)}

    def _requested_abandon(self, state, now):
        """#6978 : applique, sous le bail de ce détenteur, l'abandon qu'un
        pilote a demandé sans pouvoir prendre ce bail (`train_cli abandon`).

        Mêmes gardes que l'abandon direct, revérifiées par le stockage dans la
        même transaction : plan exact, ni phase terminale (sauf `paused`) ni
        `promoting`, aucun feu actif. Une demande périmée (autre plan, phase
        devenue terminale ou promotion) ne fait rien ; une demande refusée
        (feu donné depuis) est marquée `refused` et signalée, le train continue.
        Retourne None quand le pas suit son cours."""
        request, phase = state["abandon_request"], state.get("phase")
        plan = state.get("plan") or {}
        abandon = getattr(self.ports, "abandon", None)
        if not isinstance(request, dict) or request.get("refused") \
                or request.get("plan") != plan.get("id") \
                or phase == "promoting" or (phase in TERMINAL and phase != "paused") \
                or not callable(abandon):
            return None
        self._held()
        terminal = copy.deepcopy(state)
        terminal["phase"] = "invalidated"
        terminal["abandoned"] = dict(actor=request.get("actor"), at_ts=request.get("at_ts"),
                                     reason=request.get("reason", ""), previous_phase=phase,
                                     plan=plan["id"], fire=request.get("fire"),
                                     applied_ts=now)
        if phase == "paused":
            # #8096 : un abandon de plan en pause est un créneau consommé sans
            # promotion ; la recomposition est due sans attendre le calendrier.
            terminal["recompose"] = dict(at_ts=now, previous_phase="paused",
                                         plan=plan["id"],
                                         reason=request.get("reason", ""))
        terminal["abandon_request"] = None
        try:
            saved = abandon(terminal, int(state.get("revision", 0)), self.epoch)
        except TrainError as exc:
            # Un objet, pas None : `train_save` garderait sinon la demande.
            state["abandon_request"] = dict(request, refused=str(exc))
            state = self._save(state)
            self.ports.notify("abandon request refused under the lease: %s" % exc, state)
            return {"result": "abandon-refused", "error": str(exc), "state": state}
        self.ports.notify("plan abandoned at the pilot's request", saved)
        return {"result": "abandoned", "plan": saved["plan"], "abandoned": saved["abandoned"],
                "state": saved}

    def _members_delivered(self, state):
        """#7885 : chaque membre du plan est-il déjà livré hors du train ?

        Fait de cycle de vie, en lecture seule : un plan dont tous les membres
        ont atteint un état terminal de livraison (`delivered`, `merged`,
        `promoted`, `closed`) ne peut plus être promu. Un banc sans ce port ne
        prouve rien et garde le comportement historique (attente)."""
        check = getattr(self.ports, "delivered", None)
        plan = (state or {}).get("plan")
        if not callable(check) or not isinstance(plan, dict):
            return False
        return bool(check(plan))

    @staticmethod
    def _paused_named_culprits(state, plan):
        """#8096 : fautifs NOMMÉS par le diagnostic du plan paused.

        Un rouge multi-lots sans attribution ne prouve aucun coupable : retirer
        un innocent ne dit rien du lot resté en cause (B cx2 #45452). Seules
        deux preuves nomment un lot : `diagnosis.attribution.proven_lots` du
        rapport CI, et un plan de diagnostic dont l'unique suspect est le lot
        testé. Rend l'ensemble des identifiants nommés (vide sans preuve)."""
        named = set()
        diagnosis = (state or {}).get("diagnosis")
        attribution = diagnosis.get("attribution") if isinstance(diagnosis, dict) else None
        proven = attribution.get("proven_lots") if isinstance(attribution, dict) else None
        if isinstance(proven, list):
            for entry in proven:
                lot = entry.get("work_item_id") if isinstance(entry, dict) else None
                if isinstance(lot, int) and not isinstance(lot, bool):
                    named.add(lot)
        suspects = plan.get("suspects")
        if plan.get("diagnostic") and isinstance(suspects, list) and len(suspects) == 1 \
                and isinstance(suspects[0], dict):
            lot = suspects[0].get("work_item_id")
            if isinstance(lot, int) and not isinstance(lot, bool):
                named.add(lot)
        return named

    def _paused_members_left(self, state):
        """#8096 : lots dont le départ prouve que le plan paused est mort.

        Un plan en pause après un rouge ne sera jamais promu tel quel. Le
        service ne l'invalide que sur une preuve (B cx2 #45452, #45460) :
        - tous ses membres ont quitté `validated` (plus rien à composer) ;
        - sinon, tous les fautifs NOMMÉS par le diagnostic ou par la
          déclaration complète `train-faults: <plan> lots=[<id>,...];` du journal
          (`plan_faults`) ont quitté : un retrait partiel, un marqueur posé lot
          par lot, ou le retrait d'un innocent alors qu'un fautif reste, ne
          prouve rien et garde la pause.
        Le retour est la liste des lots partis, ou None sans preuve suffisante
        (ou lecture incomplète : port sans `entries`, document ou erreur),
        fail-closed.
        """
        plan = (state or {}).get("plan")
        members = plan.get("members") if isinstance(plan, dict) else None
        if not isinstance(members, list) or not members:
            return None
        entries = getattr(self.ports, "entries", None)
        if not callable(entries):
            return None
        try:
            current = {row["work_item_id"]: row for row in entries()
                       if isinstance(row, dict)}
        except (TrainError, KeyError, TypeError):
            return None
        if not all(isinstance(row, dict) for row in members):
            return None
        left, remaining = [], []
        for row in members:
            try:
                fresh = current.get(row["work_item_id"])
                same = fresh is not None and identity(fresh) == identity(row)
                lot_id = row["work_item_id"]
            except (KeyError, TypeError):
                return None
            (remaining if same else left).append(lot_id)
        if not left:
            return None
        if not remaining:
            # Le plan entier a quitté : aucune recomposition ne peut le
            # qualifier, et il n'y a plus de coupable à attendre.
            return left
        try:
            named = self._paused_named_culprits(state, plan)
            faults = getattr(self.ports, "plan_faults", None)
            if callable(faults):
                named |= set(faults(plan))
            member_ids = {int(row["work_item_id"]) for row in members}
        except (TrainError, KeyError, TypeError, ValueError):
            return None
        if named and named <= member_ids and named <= set(left):
            return left
        return None

    def _paused_invalidated(self, state, left, now):
        """#8096 : invalide un plan paused dont un membre n'est plus admissible.

        Écrit sous le bail de la passe, avec la trace des lots partis, puis
        rend l'époque : la passe suivante recompose sur la tête courante, sans
        eux. Aucune promotion n'est possible depuis une pause. Le marqueur
        `recompose` rend cette recomposition due sans attendre le créneau
        suivant (§11 bis) ; un plan en cours de CI n'entre jamais ici.
        """
        plan = state.get("plan") or {}
        why = ("paused plan can no longer be promoted: member(s) %s left validated, "
               "were removed or requeued; invalidated without promotion (#8096)"
               % ", ".join("#%s" % lot for lot in left))
        state["error"] = why
        state["invalidated"] = dict(at_ts=now, previous_phase="paused", reason=why,
                                    members=list(left), plan=plan.get("id"))
        state["recompose"] = dict(at_ts=now, previous_phase="paused", plan=plan.get("id"),
                                  reason=why)
        state["phase"] = "invalidated"
        state = self._save(state)
        self.ports.notify(why, state)
        return {"result": "invalidated", "error": why, "plan": state["plan"],
                "invalidated": state["invalidated"]}

    @staticmethod
    def _recompose_pending(state):
        """#8096 : ce plan terminal a-t-il consommé un créneau sans promotion ?

        Un plan paused invalidé par le service porte `recompose`; un abandon
        de pilote le porte aussi (et les documents antérieurs au déploiement
        le disent par `abandoned.previous_phase == "paused"`). Dans les deux
        cas la recomposition est due immédiatement, une seule CI de paquet à
        la fois restant garantie par la réservation de la cible.
        """
        if not isinstance(state, dict):
            return False
        if isinstance(state.get("recompose"), dict):
            return True
        abandoned = state.get("abandoned")
        return isinstance(abandoned, dict) and abandoned.get("previous_phase") == "paused"

    def _chain_pending(self, state, now, rows):
        """#8304 : le paquet suivant est-il dû hors du créneau compilé ?

        Canon §11 bis (décision owner #154026 a) : dès la fusion du précédent,
        le paquet suivant part si un lot est prêt, sans attendre le créneau ;
        l'heure reste un rythme quand rien n'est prêt. La fenêtre est l'heure
        du `Policy` : passé ce délai, la fusion n'ouvre plus le créneau et le
        calendrier reprend — sauf pour un candidat déjà prêt quand la fusion a
        été vue (B cx2 #234583 : un redémarrage tardif ne perd pas ce dû non
        consommé). Un plan invalidé a consommé son créneau : le remplacement
        n'est dû que si la composition courante diffère de celle qui vient
        d'échouer (membre parti, lot neuf, report levé), jamais pour le plan
        identique qui serait refusé à l'identique. La comparaison porte sur
        l'ensemble composable (admission à `now`, puis ordre et dépendances),
        pas sur la file brute : un lot bloqué ou non admissible ne fait pas à
        lui seul une composition nouvelle (B impl-12 #234582).
        """
        if not isinstance(state, dict) or not rows:
            return False
        plan = state.get("plan")
        members = plan.get("members") if isinstance(plan, dict) else None
        try:
            planned = ({identity(row) for row in members}
                       if isinstance(members, list) else set())
        except (KeyError, TypeError):
            return False
        phase = state.get("phase")
        if phase == "landed":
            at = state.get("landed_ts")
            if isinstance(at, bool) or not isinstance(at, (int, float)) \
                    or not math.isfinite(at) or at > now:
                return False
            if now - at <= self.policy.interval_s:
                return True
            return self._chain_ready_since_landing(state, rows)
        if phase == "invalidated":
            if not isinstance(members, list) or not members:
                return False
            # Un report de composition fait partie des entrées du plan
            # invalidé : le même lot reporté ne rend pas la composition
            # « nouvelle » à chaque passe.
            try:
                planned |= {identity(row) for row in state.get("deferred") or []
                            if isinstance(row, dict)}
            except (KeyError, TypeError):
                return False
            try:
                current = {identity(row) for row in ordered(
                    _admitted(self.ports, rows, now),
                    self.ports.dependencies(rows))}
            except (KeyError, TypeError, ValueError):
                return False
            return current != planned
        return False

    def _landing_ready(self, now, plan):
        """#8304 B cx2 #234583 : les candidats déjà prêts à la fusion vue.

        L'ensemble est figé au moment de la fusion (identités des lignes
        composables, membres du paquet exclus) : un redémarrage tardif garde
        le dû non consommé pour eux, sans rouvrir la fenêtre à une admission
        qui n'existait pas encore. Une lecture impossible rend le calendrier.
        """
        members = plan.get("members") if isinstance(plan, dict) else None
        if not isinstance(members, list):
            return []
        try:
            delivered = {identity(row) for row in members}
            fresh = [row for row in self.ports.entries()
                     if identity(row) not in delivered]
            ready = ordered(_admitted(self.ports, fresh, now),
                            self.ports.dependencies(fresh))
            return [list(identity(row)) for row in ready]
        except (KeyError, TypeError, ValueError, TrainError):
            return []

    def _chain_ready_since_landing(self, state, rows):
        """B cx2 #234583 : le dû tracé à la fusion est-il encore en file ?

        Les identités figées à la fusion et encore présentes gardent le paquet
        dû au-delà de l'heure ; une ligne arrivée ensuite ne compte pas.
        """
        stored = state.get("landed_ready")
        if not isinstance(stored, list) or not stored:
            return False
        try:
            current = {identity(row) for row in rows}
            ready = {tuple(item) for item in stored
                     if isinstance(item, (list, tuple))}
        except (KeyError, TypeError):
            return False
        return bool(current & ready)

    @staticmethod
    def _chain_candidates(state, rows):
        """#8304 : les membres du paquet livré ne sont plus candidats.

        Le premier passage après la fusion peut précéder le retrait de ses
        lignes de file. Les exclure par `identity` (jamais par numéro de lot)
        évite de recomposer un lot déjà livré tout en laissant un nouveau gel
        du même lot entrer comme une admission neuve.
        """
        if not isinstance(state, dict) or state.get("phase") != "landed":
            return rows
        plan = state.get("plan")
        members = plan.get("members") if isinstance(plan, dict) else None
        if not isinstance(members, list):
            return []
        try:
            delivered = {identity(row) for row in members}
            return [row for row in rows if identity(row) not in delivered]
        except (KeyError, TypeError):
            return []

    def _expired_step(self, state, deadline):
        """#7967 : termine un plan expiré seulement sur une issue établie.

        La réservation morte interdit toute promotion, et son échéance ne
        prouve pas que les runs dûs sont finis. Trois cas :
        - refus terminal d'une suite (runs terminés) : plan invalidé, sans
          promotion ;
        - CI `pending` (ou `absent` mais publiée, donc peut-être dispatchée),
          ou lecture impossible : le plan reste en `draining`, la cible
          reste tenue, et la passe suivante relira — jamais deux CI de
          paquet en parallèle (§11 bis) ;
        - CI terminale (`green`/`red`) ou `absent` sans publication ni feu :
          plan invalidé sans promotion, la coupure due recompose.
        """
        try:
            ci = self._ci(state)
        except SuiteRefused as exc:
            return self._drained_terminal(state, str(exc), stage="ci")
        except TrainError as exc:
            # Lecture impossible : rien ne prouve la fin des runs.
            result = {"result": "draining", "plan": state["plan"],
                      "error": "reservation expired, CI unreadable: %s" % exc}
            if isinstance(exc, TrainObjectMissing):
                result["incident"] = dict(exc.incident)    # #8480
            return result
        if ci["status"] == "absent" and not state.get("fire") \
                and self._unpublished(state):
            return self._drained_terminal(
                state, self._expired_reason(state, deadline)
                + "; no publication and no CI run: no run can still be owed")
        if ci["status"] in ("pending", "absent"):
            return {"result": "draining", "plan": state["plan"]}
        return self._drained_terminal(
            state, self._expired_reason(state, deadline)
            + "; terminal CI %s proven before invalidation" % ci["status"])

    def _expired_reservation(self, state, now):
        """#7967 : échéance absolue du ticket de réservation, ou None.

        Fail-closed : un document sans `reservation` lisible, une échéance
        absente, non finie, booléenne ou encore future ne prouvent pas une
        expiration. Lecture seule : aucun effet, aucune promotion."""
        saved = state.get("reservation")
        if not isinstance(saved, dict):
            return None
        deadline = saved.get("deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
                or not math.isfinite(deadline) or deadline > now:
            return None
        return deadline

    def _expired_reason(self, state, deadline):
        """#7967 : la raison nommée de l'invalidation, pour le diagnostic."""
        from datetime import datetime, timezone
        at = datetime.fromtimestamp(deadline, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        error = state.get("error") or "none"
        return ("packet reservation expired at %s: phase %s, last error: %s; "
                "plan invalidated without promotion (#7967)"
                % (at, state.get("phase"), error))

    def _drained_terminal(self, state, why, *, stage="draining"):
        """#7885 : termine un plan en attente qui n'aura jamais de résultat.

        `invalidated` est terminal : le `finally` de `step` rend l'époque et
        la réservation. Le diagnostic persiste la raison et la notification
        suit, comme pour un refus de publication. Aucune promotion.
        `stage` nomme l'origine (#7967 : `ci` pour un refus de suite terminé)."""
        state["error"] = why
        state["diagnosis"] = {"stage": stage, "why": why}
        state["phase"] = "invalidated"
        state = self._save(state)
        self.ports.notify(why, state)
        return {"result": "invalidated", "error": why, "plan": state["plan"]}

    def _unpublished(self, state):
        """#6978 (B cx2 #213473) : la preuve qu'aucune passe n'a publié le plan.

        Le push de publication est atomique (référence immuable du train et
        bail ensemble) et c'est lui qui déclenche la CI : une référence lue
        absente, sous notre bail, prouve qu'aucun effet distant n'a eu lieu,
        quel que soit l'historique du document (marqueur absent, document
        antérieur à ce déploiement). Lecture impossible ou référence présente :
        rien n'est prouvé, le plan garde `publishing` et sa réservation."""
        published = getattr(self.ports, "published", None)
        if not callable(published):
            return False
        try:
            return published(state["plan"]) == ""
        except TrainError:
            return False

    def _publication_refused(self, state, exc):
        """#6978 : termine un plan dont la publication est refusée à l'identique.

        `invalidated` est terminal : le `finally` de `step` rend l'époque et
        la réservation ; le créneau suivant recompose sur la tête courante.
        Le diagnostic nomme le refus (contrat CI, suite due absente)."""
        state["diagnosis"] = {"stage": "publication", "why": str(exc)}
        state["error"] = str(exc)
        state["phase"] = "invalidated"
        state = self._save(state)
        self.ports.notify("publication refused before any remote effect: %s" % exc, state)
        return {"result": "invalidated", "error": str(exc), "plan": state["plan"]}

    def _red(self, state, now):
        state["failures"] += 1
        state["diagnosis"] = copy.deepcopy(state["attempts"][-1]["ci"].get("diagnosis"))
        members = state["plan"]["suspects"]
        if self.policy.build_beta:
            # One complete CI per cutoff. A multi-lot failure alone proves
            # no culprit; require factual diagnosis without dispatching
            # diagnostic halves or silently transferring a green result.
            state["phase"] = "paused"
            state = self._save(state)
            self.ports.notify("build/beta packet CI red (plan %s): factual diagnosis "
                              "unknown; no automatic diagnostic CI. To declare the "
                              "complete set of culprits, write a note beginning "
                              "train-faults: %s lots=[<lot>,<lot>]; <reason>; the "
                              "pause lifts only when every declared culprit has left."
                              % (state["plan"]["id"][:8], state["plan"]["id"][:8]), state)
            return {"result": "paused"}
        if state["failures"] >= self.policy.max_failures:
            state["phase"] = "paused"
            state = self._save(state)
            self.ports.notify("repeated train CI failures", state)
            return {"result": "paused"}
        if len(members) == 1:
            # CI on the singleton is proof, never a guess from a failed job
            # name or an overlapping impact-map entry.
            self._held()
            self.ports.quarantine(members, "complete CI red on isolated member and its "
                                   "proven prerequisite context", self.epoch)
            state["excluded"].extend(members)
        else:
            middle = len(members) // 2
            state["pending"] = [members[:middle], members[middle:]] + state["pending"]
        return self._next_probe(state, now)

    def _next_probe(self, state, now):
        while state["pending"]:
            suspects = state["pending"].pop(0)
            # Test a half with its prerequisites. A removed prerequisite
            # defers dependents, without falsely blaming or ejecting them.
            excluded = {r["work_item_id"] for r in state["excluded"]}
            candidates = [r for r in state["members"] if r["work_item_id"] not in excluded]
            deps = self.ports.dependencies(candidates)
            available = ordered(candidates, deps)
            available_ids = {r["work_item_id"] for r in available}
            state["deferred"].extend(r for r in suspects if r["work_item_id"] not in available_ids)
            suspects = [r for r in suspects if r["work_item_id"] in available_ids]
            if not suspects:
                continue
            wanted = {r["work_item_id"] for r in suspects}
            changed = True
            while changed:
                more = {int(e["blocker_id"]) for e in deps
                        if e["blocking"] and int(e["work_item_id"]) in wanted}
                changed = bool(more - wanted)
                wanted.update(more)
            members = [r for r in available if r["work_item_id"] in wanted]
            state = self._plan(state, members, now, diagnostic=True, suspects=suspects)
            return {"result": "recomposed", "plan": state["plan"]}
        # Two green halves can fail together. If no singleton was excluded,
        # preserve the interaction evidence and ask the pilot; never cycle.
        if not state["excluded"]:
            state["phase"] = "paused"
            state = self._save(state)
            self.ports.notify("train interaction: green halves, red union", state)
            return {"result": "paused"}
        # Prerequisites removed by isolation remove their dependents as well.
        unique = list({r["work_item_id"]: r for r in state["good"]}.values())
        members = ordered(unique, self.ports.dependencies(unique))
        if not members:
            state["phase"] = "empty"
            state = self._save(state)
            return {"result": "empty"}
        state["good"], state["excluded"], state["pending"] = [], [], []
        # Subsequent diagnostics concern this new union only. Previously
        # quarantined members must never reappear as prerequisite context.
        state["members"] = members
        # Even if this set equals a green diagnostic half, compose a new plan
        # and run complete CI on its own head before the actual promotion.
        # #8305: this plan comes from members of an already preflighted
        # composition; a MemberDeferred here has no composing loop to catch it,
        # so the unchanged publication gate keeps the last word.
        state = self._plan(state, members, now, preflight=False)
        return {"result": "recomposed", "plan": state["plan"]}
