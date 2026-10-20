# SPDX-License-Identifier: AGPL-3.0-only
"""#2814 : fournisseur de capacité enfichable pour l'admission d'un exécuteur.

Sous pression, ameesh ne savait que différer un tour (`#419` avait rendu la
pause effective). Dans un cluster autoscalé la bonne réaction est d'OBTENIR de
la capacité : ce module donne à l'hôte un point d'extension, pas une politique
de cluster. Le fournisseur par défaut (`NoopProvider`) ne fait rien et le
comportement d'avant est conservé.

Un fournisseur reçoit des ÉVÉNEMENTS (`need`, `release`) en JSON et rend une
décision (`granted`, `wait`, `none`, `refused`, `error`) ; il ne reçoit ni
secret, ni identifiant de modèle, ni budget détaillé. Deux formes fournies :

* `cmd:<argv…>` : une commande est lancée (sans shell) avec l'événement sur
  son entrée standard ; sa dernière ligne de sortie est le JSON de décision.
  C'est le contrat d'un scaler externe : « capacité nécessaire / libérée » ;
* `<module>:<classe>` : une classe Python est importée et instanciée ; elle doit
  exposer `need(event)` et `release(event)` et rendre un mapping. Ses appels
  sont bornés par le même délai que `cmd:` (#2825) : un module qui pend ne
  retient pas le worker.

Une décision `granted` ne lance AUCUN tour : le fournisseur peut ajouter de
la capacité (pod, nœud, quota), et c'est la pression qui retombe qui ouvre le
tour, sous le bail et l'epoch de l'agent (une seule session vivante). Les
bornes restent celles de l'exécuteur :

* au plus UNE demande en cours (`Manager._outstanding`) ;
* `min_interval_s` entre deux demandes (15 s) ;
* `needs_per_hour` (4 par défaut) : pas d'extension sans fin ;
* `release_after_s` (60 s) de calme avant l'événement `release` ;
* le fournisseur est appelé par un worker qui a déjà vérifié le budget
  (`AgentWorker.budget_ok`) — un dépassement de budget ferme la porte, pas le
  fournisseur ;
* une erreur, un délai de `cmd:` ou une sortie illisible laisse la pression
  telle quelle (fail closed : rien n'est accordé) ;
* un appel de module qui dépasse `AMEESH_CAPACITY_PROVIDER_TIMEOUT` garde son
  slot (`requesting`/`releasing`) ; sa réponse tardive est appliquée au
  prochain `need`/`observe`, avec le même `capacity_id`, et aucun second appel
  ne démarre tant que l'appel est en vol (#2825, verdict cx2) ;
* un `refused`, `error` ou `none` ne garde pas le slot `requested` (#2825) :
  la fenêtre suivante peut redemander ; un `release` n'est émis qu'avec un
  `capacity_id` réellement accordé.

L'observabilité : chaque décision est journalisée (`log_async`) et le dernier
état est repris dans le texte de statut de l'agent (`resources.queue_text`),
donc dans `ameesh show`/`ameesh list` et `GET /api/v1/agents`.
"""
from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import threading
import time

SCHEMA = "ameesh-capacity/1"
EVENTS = ("need", "release")
ACTIONS = ("granted", "wait", "none", "refused", "error")
#: release replies that CONFIRM the capacity is gone; anything else (wait,
#: refused, unknown, missing action) keeps the slot `release_failed` and is
#: retried with the same `capacity_id`
RELEASE_CONFIRMATIONS = ("none", "granted", "released")
#: borne de la réponse d'un fournisseur lue sur sa sortie standard
MAX_REPLY_BYTES = 4096
#: délai d'un appel de fournisseur (secondes)
DEFAULT_TIMEOUT_S = 5.0
#: au plus une demande en cours, espacée, et bornée par heure
DEFAULT_MIN_INTERVAL_S = 15.0
DEFAULT_NEEDS_PER_HOUR = 4
DEFAULT_RELEASE_AFTER_S = 60.0
#: a failed release is retried, with the SAME capacity_id, after this delay;
#: the slot stays occupied (no new need) until the provider confirms
DEFAULT_RELEASE_RETRY_S = 30.0
#: bounded retries: after that the slot stays `release_failed` (an operator
#: or a restart is needed), so a new scale-out is never admitted
DEFAULT_MAX_RELEASE_ATTEMPTS = 5
PROVIDER_ENV = "AMEESH_CAPACITY_PROVIDER"
TIMEOUT_ENV = "AMEESH_CAPACITY_PROVIDER_TIMEOUT"
MIN_INTERVAL_ENV = "AMEESH_CAPACITY_MIN_INTERVAL"
NEEDS_PER_HOUR_ENV = "AMEESH_CAPACITY_NEEDS_PER_HOUR"
RELEASE_AFTER_ENV = "AMEESH_CAPACITY_RELEASE_AFTER"
RELEASE_RETRY_ENV = "AMEESH_CAPACITY_RELEASE_RETRY"
#: valeurs magiques qui désactivent explicitement le fournisseur
DISABLED = ("", "none", "noop", "off", "0")


def _number(value, default: float, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number < minimum:  # NaN, or negative
        return default
    return number


def _log(log, text: str) -> None:
    if log is None:
        return
    try:
        log(text)
    except Exception:  # a logging failure never changes the decision
        pass


def capacity_id(value) -> str | int | float | None:
    """L'identifiant d'une capacité RÉELLEMENT accordée, sinon None (#2825).

    Un fournisseur qui répond `granted` sans `capacity_id` n'a rien accordé
    d'identifiable : on ne peut ni le libérer ni le représenter à l'identique.
    Un identifiant vide, booléen ou d'un autre type ne compte pas."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if value != value:  # NaN
        return None
    return value


def event(kind: str, **fields) -> dict:
    """Un événement `ameesh-capacity/1` : `event` (`need`|`release`), hôte,
    agent, portée de pression, franchissements, budget et plafond d'agents."""
    out = {"schema": SCHEMA, "event": kind}
    out.update({key: value for key, value in fields.items() if value is not None})
    return out


class Provider:
    """Le contrat minimal d'un fournisseur (`need`, `release`)."""

    is_noop = False

    def need(self, event: dict) -> dict:
        return {"action": "wait"}

    def release(self, event: dict) -> dict:
        return {"action": "none"}


class NoopProvider(Provider):
    """Le défaut : aucune capacité demandée, aucune capacité rendue."""

    is_noop = True

    def need(self, event: dict) -> dict:
        return {"action": "none"}

    def release(self, event: dict) -> dict:
        return {"action": "none"}


class CommandProvider(Provider):
    """Un scaler externe, appelé sans shell avec l'événement sur stdin.

    Chaque ligne de sortie est essayée en JSON ; la dernière ligne lisible
    est la décision. Une commande absente, un code de sortie non nul, un
    délai dépassé ou une sortie illisible rendent `error` : la pression reste
    la seule règle, rien n'est accordé par défaut."""

    def __init__(self, argv, *, timeout: float = DEFAULT_TIMEOUT_S, log=None) -> None:
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError("capacity provider: a non-empty argv is required")
        self.argv = [str(part) for part in argv]
        self.timeout = _number(timeout, DEFAULT_TIMEOUT_S, minimum=0.1)
        self.log = log

    def _run(self, payload: dict) -> dict:
        try:
            proc = subprocess.run(
                self.argv, input=json.dumps(payload, ensure_ascii=False),
                text=True, capture_output=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            _log(self.log, "capacity: provider %s failed (%s)"
                 % (self.argv[0], type(exc).__name__))
            return {"action": "error", "error": type(exc).__name__}
        if proc.returncode != 0:
            _log(self.log, "capacity: provider %s exited %d"
                 % (self.argv[0], proc.returncode))
            return {"action": "error", "error": "exit %d" % proc.returncode}
        text = (proc.stdout or "")[:MAX_REPLY_BYTES]
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict):
                return reply
        _log(self.log, "capacity: provider %s gave no readable JSON decision"
             % self.argv[0])
        return {"action": "error", "error": "unreadable reply"}

    def need(self, event: dict) -> dict:
        return self._run(dict(event, event="need"))

    def release(self, event: dict) -> dict:
        return self._run(dict(event, event="release"))


class ModuleProvider(Provider):
    """Un fournisseur `<module>:<classe>`, borné comme le fournisseur `cmd:`.

    La classe importée n'est pas un sous-processus : ses méthodes rendent la
    main ou bloquent l'exécuteur. Chaque appel est attendu au plus
    `AMEESH_CAPACITY_PROVIDER_TIMEOUT` (5 s par défaut) ; au-delà, le SLOT
    reste tenu (`pending`) et l'appel continue. `poll()` rend sa réponse
    tardive au Manager, qui la traite comme la réponse synchrone (grant →
    `requested`, release confirmée → libéré). Aucun second appel n'est lancé
    tant que l'appel en vol n'a pas rendu la main : deux `need` ne peuvent ni
    accorder deux capacités ni en perdre une (#2825, verdict cx2)."""

    def __init__(self, provider, *, timeout: float = DEFAULT_TIMEOUT_S, log=None) -> None:
        self.provider = provider
        self.timeout = _number(timeout, DEFAULT_TIMEOUT_S, minimum=0.1)
        self.log = log
        self.is_noop = bool(getattr(provider, "is_noop", False))
        self._lock = threading.Lock()
        self._seq = 0
        self._inflight: dict | None = None

    def _call(self, method: str, event: dict) -> dict:
        with self._lock:
            current = self._inflight
            if current is not None:
                # The Manager serializes calls; defensive only: the call in
                # flight keeps its token and the slot stays occupied.
                return {"action": "wait", "pending": True,
                        "token": current["token"],
                        "detail": "provider call in flight"}
            self._seq += 1
            token = self._seq
            box: dict = {}

            def run() -> None:
                try:
                    box["reply"] = getattr(self.provider, method)(dict(event))
                except Exception as exc:  # a provider never takes us down
                    box["reply"] = {"action": "error",
                                    "error": type(exc).__name__}

            thread = threading.Thread(target=run, daemon=True,
                                      name="capacity-%s" % method)
            self._inflight = {"token": token, "method": method,
                              "thread": thread, "box": box, "waiting": True}
        thread.start()
        thread.join(float(self.timeout))
        with self._lock:
            inflight = self._inflight
            if inflight is None or inflight["token"] != token:
                return {"action": "error", "error": "call lost"}
            inflight["waiting"] = False
            if thread.is_alive():
                _log(self.log, "capacity: %s provider still running after %gs "
                     "(slot kept; the late reply will be polled)"
                     % (method, float(self.timeout)))
                return {"action": "wait", "pending": True, "token": token,
                        "detail": "provider call in flight"}
            self._inflight = None
            reply = box.get("reply")
        return reply if isinstance(reply, dict) else {"action": "error"}

    def poll(self) -> dict | None:
        """La réponse d'un appel qui a dépassé le délai, ou None (#2825).

        Ne rend jamais un appel encore en vol, ni un appel qu'un `need`/
        `observe` synchrone est en train d'attendre (`waiting`)."""
        with self._lock:
            inflight = self._inflight
            if inflight is None or inflight["waiting"]:
                return None
        if inflight["thread"].is_alive():
            return None
        with self._lock:
            if self._inflight is not inflight:
                return None
            self._inflight = None
            box = inflight["box"]
        reply = box.get("reply")
        if not isinstance(reply, dict):
            reply = {"action": "error"}
        return {"token": inflight["token"], "method": inflight["method"],
                "reply": reply}

    def need(self, event: dict) -> dict:
        return self._call("need", event)

    def release(self, event: dict) -> dict:
        return self._call("release", event)


def load_provider(spec: str | None = None, *, log=None) -> Provider:
    """Le fournisseur configuré (`AMEESH_CAPACITY_PROVIDER`), ou le no-op.

    `cmd:<argv…>` : `CommandProvider` (`shlex`, variables d'environnement
    développées) ; `<module>:<classe>` : une classe importée ; tout le reste
    retombe sur le no-op avec une ligne de journal (une faute de frappe ne
    doit pas arrêter l'exécuteur ni accorder une capacité)."""
    spec = (spec if spec is not None else os.environ.get(PROVIDER_ENV) or "").strip()
    if spec.lower() in DISABLED:
        return NoopProvider()
    timeout = _number(os.environ.get(TIMEOUT_ENV), DEFAULT_TIMEOUT_S, minimum=0.1)
    if spec.startswith("cmd:"):
        try:
            argv = [os.path.expandvars(part) for part in shlex.split(spec[4:])]
            return CommandProvider(argv, timeout=timeout, log=log)
        except ValueError as exc:
            _log(log, "capacity: unusable command (%s); no provider" % exc)
            return NoopProvider()
    if ":" in spec:
        module_name, _, attr = spec.partition(":")
        try:
            module = importlib.import_module(module_name)
            factory = getattr(module, attr) if attr else module
            provider = factory() if callable(factory) else factory
            if not callable(getattr(provider, "need", None)):
                raise TypeError("need(event) missing")
            if not callable(getattr(provider, "release", None)):
                raise TypeError("release(event) missing")
            return ModuleProvider(provider, timeout=timeout, log=log)
        except Exception as exc:
            _log(log, "capacity: provider %r unusable (%s); no provider"
                 % (spec, type(exc).__name__))
            return NoopProvider()
    _log(log, "capacity: provider %r unknown (cmd:… or module:class); no provider"
         % spec)
    return NoopProvider()


class Manager:
    """Les bornes d'un fournisseur : une demande à la fois, espacée, bornée
    par heure, et une libération après `release_after_s` de calme.

    Le Manager ne lance aucun tour et ne touche pas aux baux : il signale.
    Chaque décision est journalisée et reprise dans le statut de l'agent."""

    def __init__(self, provider: Provider | None = None, *, log=None,
                 now=time.time, min_interval_s: float | None = None,
                 needs_per_hour: int | None = None,
                 release_after_s: float | None = None) -> None:
        self.provider = provider if provider is not None else load_provider(log=log)
        self.log = log
        self._clock = now
        self.min_interval_s = _number(
            os.environ.get(MIN_INTERVAL_ENV) if min_interval_s is None else min_interval_s,
            DEFAULT_MIN_INTERVAL_S)
        self.release_after_s = _number(
            os.environ.get(RELEASE_AFTER_ENV) if release_after_s is None else release_after_s,
            DEFAULT_RELEASE_AFTER_S)
        self.release_retry_s = _number(os.environ.get(RELEASE_RETRY_ENV),
                                       DEFAULT_RELEASE_RETRY_S)
        self.max_release_attempts = DEFAULT_MAX_RELEASE_ATTEMPTS
        try:
            per_hour = int(os.environ.get(NEEDS_PER_HOUR_ENV)
                           if needs_per_hour is None else needs_per_hour)
        except (TypeError, ValueError):
            per_hour = DEFAULT_NEEDS_PER_HOUR
        self.needs_per_hour = max(0, per_hour)
        self._lock = threading.Lock()
        self._outstanding: dict | None = None
        self._last_need = 0.0
        self._needs: list[float] = []
        self._idle_since: float | None = None

    @property
    def enabled(self) -> bool:
        return not getattr(self.provider, "is_noop", False)

    def _now(self) -> float:
        return float(self._clock())

    def _clean(self, now: float) -> None:
        self._needs = [ts for ts in self._needs if now - ts < 3600.0]

    @staticmethod
    def _need_action(reply: dict) -> tuple[str, object, str | None]:
        action = reply.get("action")
        if action not in ACTIONS:
            action = "error"
        granted = capacity_id(reply.get("capacity_id"))
        detail = reply.get("detail") or reply.get("error")
        if action == "granted" and granted is None:
            # #2825 N1: a grant without an identifier cannot be released nor
            # represented with the same id later; it is not a usable capacity
            action = "error"
            detail = detail or "granted without capacity_id"
        return action, granted, detail

    def _need_state(self, state: dict, reply: dict) -> tuple[dict, str]:
        """L'état d'une demande qui vient de rendre (ou de rendre tardivement)."""
        action, granted, detail = self._need_action(reply)
        settled = dict(state)
        settled.update({"action": action, "capacity_id": granted,
                        "detail": detail, "pending": False})
        settled.pop("call", None)
        settled["state"] = "requested" if action in ("granted", "wait") else "idle"
        return settled, action

    def _release_result(self, state: dict, reply: dict, now: float):
        """(nouvel état ou None, résultat rendu à l'appelant) d'un release."""
        action = reply.get("action")
        # Explicit confirmation only: a provider that says `wait`, `refused`,
        # gives no action or an unknown one has NOT released the capacity.
        failed = not (isinstance(action, str) and action in RELEASE_CONFIRMATIONS)
        if not failed:
            return None, {"state": "released", "action": action,
                          "since": state.get("since"), "agent": state.get("agent"),
                          "host": state.get("host"),
                          "capacity_id": state.get("capacity_id")}
        failed_state = dict(state)
        failed_state.update({
            "state": "release_failed", "action": action or "error",
            "detail": reply.get("detail") or reply.get("error")
                      or "release not confirmed (%r)" % (action,),
            "pending": False})
        failed_state.pop("call", None)
        if failed_state["release_attempts"] >= self.max_release_attempts:
            failed_state["next_retry"] = None
        else:
            failed_state["next_retry"] = now + self.release_retry_s
        return failed_state, dict(failed_state)

    def _poll(self) -> None:
        """Consomme la réponse tardive d'un fournisseur asynchrone (#2825).

        Appelé au début de `need` et `observe`, jamais sous le verrou du
        Manager : l'appel en vol garde son slot (`requesting`/`releasing`) et
        son token ; une réponse d'un ancien appel est ignorée."""
        poll = getattr(self.provider, "poll", None)
        if not callable(poll):
            return
        try:
            late = poll()
        except Exception as exc:  # a provider never takes the executor down
            _log(self.log, "capacity: late provider reply lost (%s)"
                 % type(exc).__name__)
            return
        if not isinstance(late, dict):
            return
        token = late.get("token")
        method = late.get("method")
        reply = late.get("reply")
        reply = dict(reply) if isinstance(reply, dict) else {"action": "error"}
        now = self._now()
        if method == "need":
            with self._lock:
                state = self._outstanding
                if state is None or state.get("state") != "requesting":
                    return
                if state.get("call") is not None and state.get("call") != token:
                    return
                settled, action = self._need_state(state, reply)
                if action in ("granted", "wait"):
                    self._outstanding = settled
                else:
                    self._outstanding = None
                self._idle_since = None
            _log(self.log, "capacity: late need for %s on %s -> %s%s"
                 % (settled.get("agent"), settled.get("host"), action,
                    " (%s)" % settled.get("detail")
                    if settled.get("detail") else ""))
        elif method == "release":
            with self._lock:
                state = self._outstanding
                if state is None or state.get("state") != "releasing":
                    return
                if state.get("call") is not None and state.get("call") != token:
                    return
                new, result = self._release_result(state, reply, now)
                self._outstanding = new
                if new is None:
                    self._idle_since = None
            _log(self.log, "capacity: late release for %s on %s -> %s%s (attempt %d)"
                 % (state.get("agent"), state.get("host"),
                    reply.get("action") or "error",
                    " (retry in %ds)" % int(self.release_retry_s)
                    if new is not None and new.get("next_retry") else "",
                    state.get("release_attempts") or 0))

    def need(self, *, host: str, agent: str, verdict: dict | None = None,
             budget_usd_hour: float | None = None, max_agents: int | None = None,
             queued_s: float | None = None) -> dict | None:
        """Signale une capacité nécessaire, au plus une fois par fenêtre.

        Rend l'état de la demande (y compris `action: none` du no-op), ou None
        quand le fournisseur est absent/refusé par les bornes. Un appelant
        sans budget disponible ne doit pas appeler cette méthode. Un appel de
        module qui dépasse son délai rend `state: requesting` (`pending`) : le
        slot reste tenu, aucun second appel ne démarre, et sa réponse tardive
        est consommée au prochain `need`/`observe` (#2825)."""
        if not self.enabled:
            return None
        self._poll()
        now = self._now()
        with self._lock:
            if self._outstanding is not None:
                return dict(self._outstanding)
            self._clean(now)
            if now - self._last_need < self.min_interval_s:
                return None
            if len(self._needs) >= self.needs_per_hour:
                return None
            self._last_need = now
            self._needs.append(now)
            # The placeholder is set BEFORE the provider call: two worker
            # threads must never send two scale-out requests for one calm
            # window (one outstanding request at a time).
            placeholder = {"state": "requesting", "action": None,
                           "since": now, "agent": agent, "host": host}
            self._outstanding = placeholder
        payload = event(
            "need", host=host, agent=agent, ts=now,
            scope=(verdict or {}).get("scope"),
            breaches=[{"key": b.get("key"), "critical": bool(b.get("critical")),
                       "value": b.get("value"), "limit": b.get("limit")}
                      for b in (verdict or {}).get("breaches") or ()],
            budget_usd_hour=budget_usd_hour, max_agents=max_agents,
            queued_s=None if queued_s is None else int(max(0.0, queued_s)))
        try:
            reply = self.provider.need(payload)
        except Exception as exc:  # a provider never takes the executor down
            reply = {"action": "error", "error": type(exc).__name__}
        if isinstance(reply, dict) and reply.get("pending"):
            # #2825 (verdict cx2): the call is still in flight. Keep the slot
            # and its token; the late reply will be applied by `_poll`, never
            # lost, and no second need can start meanwhile.
            with self._lock:
                if self._outstanding is placeholder:
                    placeholder.update({
                        "action": "wait",
                        "detail": reply.get("detail") or "provider call in flight",
                        "pending": True, "call": reply.get("token")})
            _log(self.log, "capacity: need for %s on %s (%s) pending: provider "
                 "call in flight" % (agent, host,
                                     (verdict or {}).get("scope") or "?"))
            return dict(placeholder)
        reply = dict(reply) if isinstance(reply, dict) else {"action": "error"}
        settled, action = self._need_state(placeholder, reply)
        with self._lock:
            if self._outstanding is placeholder:
                if action in ("granted", "wait"):
                    self._outstanding = settled
                else:
                    # #2825 N1: refused/error/none granted nothing. Drop the
                    # slot so a later window can ask again (min_interval and
                    # needs_per_hour still bound the attempts); never hold a
                    # `requested` slot that no capacity will ever fill.
                    self._outstanding = None
            self._idle_since = None
        _log(self.log, "capacity: need for %s on %s (%s) -> %s%s"
             % (agent, host, (verdict or {}).get("scope") or "?",
                action, " (%s)" % settled["detail"] if settled["detail"] else ""))
        return dict(settled)

    def observe(self, blocked: bool, *, now: float | None = None) -> dict | None:
        """Après un verdict frais : libère la capacité après le calme requis.

        Rend l'état de la libération quand elle vient d'être envoyée ou
        retentée. Une libération en échec garde le `capacity_id` et le slot
        occupé (`release_failed`) : aucune nouvelle demande n'est admise tant
        que le fournisseur n'a pas confirmé, et le même identifiant est
        représenté aux tentatives suivantes (idempotence). Après
        `max_release_attempts`, le slot reste `release_failed` : un opérateur
        (ou un redémarrage) tranche, jamais une seconde allocation. Un appel
        de module qui dépasse son délai garde `releasing` (`pending`) ; sa
        réponse tardive est appliquée par le `observe` suivant, sans chevaucher
        l'appel vivant (#2825)."""
        if not self.enabled:
            return None
        self._poll()
        now = self._now() if now is None else float(now)
        with self._lock:
            state = self._outstanding
            if state is None:
                return None
            state_name = state.get("state")
            if state_name == "requesting":
                return None  # the provider need call is still in flight
            if state_name == "releasing":
                # a release RPC is in flight: never start a second one (the
                # caller that finishes would otherwise drop a newer slot)
                return None
            if state_name == "requested":
                if blocked:
                    self._idle_since = None
                    return None
                if self._idle_since is None:
                    self._idle_since = now
                    return None
                if now - self._idle_since < self.release_after_s:
                    return None
            elif state_name == "release_failed":
                # the capacity exists: release it even if pressure came back;
                # retry only after the delay, and only a bounded number of
                # times (a failed release still holds the slot)
                if state.get("next_retry") is not None and now < state["next_retry"]:
                    return None
                if int(state.get("release_attempts") or 0) >= self.max_release_attempts:
                    return None
            else:
                return None
            if capacity_id(state.get("capacity_id")) is None:
                # #2825 N1: a `wait` reply (or a malformed grant) never gave
                # an identifiable capacity; releasing one would announce a
                # capacity we do not hold. Drop the slot without a provider
                # call so a later need can ask again.
                self._outstanding = None
                self._idle_since = None
                skipped = dict(state)
                skipped["state"] = "idle"
                state = None
            else:
                state = dict(state)
                state["state"] = "releasing"
                state["release_attempts"] = int(state.get("release_attempts") or 0) + 1
                state["release_last_at"] = now
                state["next_retry"] = None
                self._outstanding = state
        if state is None:
            _log(self.log, "capacity: release skipped for %s on %s "
                 "(no capacity_id: nothing was granted)"
                 % (skipped.get("agent"), skipped.get("host")))
            return skipped
        payload = event("release", host=state.get("host"), agent=state.get("agent"),
                        ts=now, capacity_id=state.get("capacity_id"),
                        release_attempt=state["release_attempts"],
                        sent_s=round(now - float(state.get("since") or now), 3))
        try:
            reply = self.provider.release(payload)
        except Exception as exc:
            reply = {"action": "error", "error": type(exc).__name__}
        if isinstance(reply, dict) and reply.get("pending"):
            # #2825 (verdict cx2): the release is still in flight. Keep
            # `releasing` and its token; the late reply is applied by `_poll`.
            with self._lock:
                if self._outstanding is state:
                    state["pending"] = True
                    state["detail"] = reply.get("detail") or "provider call in flight"
                    state["call"] = reply.get("token")
            _log(self.log, "capacity: release for %s on %s pending: provider "
                 "call in flight (attempt %d)"
                 % (state.get("agent"), state.get("host"),
                    state.get("release_attempts") or 0))
            return dict(state)
        reply = dict(reply) if isinstance(reply, dict) else {"action": "error"}
        action = reply.get("action")
        failed = not (isinstance(action, str) and action in RELEASE_CONFIRMATIONS)
        with self._lock:
            if self._outstanding is not state:
                # another release/need replaced the slot while this RPC was in
                # flight; never clear or overwrite someone else's allocation
                result = dict(self._outstanding or state)
            else:
                new, result = self._release_result(state, reply, now)
                self._outstanding = new
                if new is None:
                    self._idle_since = None
        _log(self.log, "capacity: release for %s on %s -> %s%s (attempt %d)"
             % (state.get("agent"), state.get("host"), action,
                " (retry in %ds)" % int(self.release_retry_s)
                if failed and result.get("next_retry") else "",
                state.get("release_attempts") or 0))
        return result

    def snapshot(self) -> dict | None:
        """L'état de la demande en cours (ou de la dernière libération), pour
        le verdict d'hôte et le texte de statut."""
        with self._lock:
            return dict(self._outstanding) if self._outstanding else None

    def forget(self) -> None:
        """Arrêt de l'exécuteur : aucune libération n'est envoyée depuis un
        processus qui s'arrête (le scaler a sa propre expiration)."""
        with self._lock:
            self._outstanding = None
            self._idle_since = None
