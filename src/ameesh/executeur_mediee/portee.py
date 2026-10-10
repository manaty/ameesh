# SPDX-License-Identifier: AGPL-3.0-only
"""Portée des opérations de l'API d'exécuteur médiée (serveur, lot L108).

`HostScopeRules` implémente `interfaces.ScopeRules` : une règle par ligne de
la table du contrat (`contrat.json`). Elle est construite pour UNE requête,
sur la transaction de l'opération : les agents admis, la disponibilité de
l'hôte et les contrôles d'appartenance (tour, lot, message, action) sont lus
dans la même transaction que l'écriture.

Agents admis sur l'hôte d'un exécuteur (`admitted_agents`) :

* l'agent est inscrit sur cet hôte (`agent_registry.host`) ;
* il est gouverné par le canon (`canon_governed_sql`) et son placement sur
  cet hôte est admis pour son profil actuel (`placement_admitted_sql`) :
  admission, politique d'hôte et visibilité 0029 sont calculées par la
  synchronisation du canon au serveur ;
* il est dans la liste blanche de l'invitation, si elle existe
  (`Principal.agents_allowlist`) ;
* son mode d'identifiants est l'un de `credential_modes`, si le serveur en
  fixe (la fiche `Host` d'un appareil tiers porte `credential_modes:
  [relay]` : le canon le garantit déjà ; ce filtre est une défense de plus).

Un agent inscrit à la main (hors canon) n'est jamais admis sur un hôte
médié. Les refus suivent `contrat.ERRORS` : `forbidden_scope` (agent, hôte,
lot ou tour hors portée), `host_unavailable` (porte d'hôte fermée),
`bad_args` (champ interdit, borne dépassée).

Le fencing (portée B, et le bail du jeton de session pour les écritures de
portée S) n'est PAS ici : le répartiteur le refait sous verrou de ligne,
dans la transaction de l'opération (`repartiteur`).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from ..storage.postgres.mailbox import PROJECT_SQL
from ..storage.postgres.registry import canon_governed_sql, placement_admitted_sql
from .contrat import Fence, Operation
from .interfaces import Principal, ScopeError, ScopeRules

#: bornes de taille des arguments (en plus de `contrat.MAX_BODY_BYTES`)
MAX_LIMIT = 200
MAX_IDS = 1000
MAX_READINGS = 100
#: bail imposé par défaut à un hôte médié (secondes), renouvelé toutes les 30 s
DEFAULT_LEASE_TTL_S = 90.0
DEFAULT_LEASE_RENEW_S = 30.0


class Immediate(Exception):
    """La règle connaît déjà la réponse : pas d'appel au pilote (ex.
    `agents.claimable` sur un hôte indisponible : liste vide)."""

    def __init__(self, value: Any):
        super().__init__("réponse immédiate")
        self.value = value


def host_available(db, host: str) -> bool:
    """La porte de l'hôte est-elle ouverte ? Dernier état rapporté par
    `PUT /host/availability` : `available` vrai et fenêtre non échue. Sans
    rapport : indisponible (prudence : l'exécuteur rapporte au démarrage)."""
    rows = db.query(
        "SELECT (available AND (until_at IS NULL OR until_at > clock_timestamp())) AS ok"
        "  FROM exec_host_availability WHERE host = %s",
        (host,))
    return bool(rows and rows[0]["ok"])


def admitted_sql() -> str:
    """Requête des agents admis sur un hôte (paramètre : l'hôte)."""
    return ("SELECT r.name, r.credential_mode FROM agent_registry r"
            " WHERE r.host = %%s AND %s AND %s ORDER BY r.name"
            % (canon_governed_sql("r"), placement_admitted_sql("r")))


class HostScopeRules(ScopeRules):
    """Les règles de portée, lues sur la transaction `db` de la requête."""

    def __init__(self, db, *, credential_modes: Optional[frozenset] = None,
                 lease_ttl_s: float = DEFAULT_LEASE_TTL_S):
        self.db = db
        self.credential_modes = credential_modes
        self.lease_ttl_s = float(lease_ttl_s)
        self._admitted: Optional[frozenset] = None

    # -- agents admis -------------------------------------------------------
    def admitted_agents(self, principal: Principal) -> frozenset:
        if self._admitted is None:
            rows = self.db.query(admitted_sql(), (principal.host,))
            names = {r["name"] for r in rows
                     if self.credential_modes is None
                     or (r.get("credential_mode") or "") in self.credential_modes}
            if principal.agents_allowlist is not None:
                names &= set(principal.agents_allowlist)
            self._admitted = frozenset(names)
        return self._admitted

    def _require_admitted(self, principal: Principal, name: Any) -> None:
        if not isinstance(name, str) or name not in self.admitted_agents(principal):
            raise ScopeError("forbidden_scope", "agent hors de la portée de l'exécuteur")

    # -- avant l'appel --------------------------------------------------------
    def apply(self, principal: Principal, op: Operation, bound: dict,
              fence: Optional[Fence]) -> dict:
        kwargs = dict(bound)
        for param, value in op.forced.items():
            if value == "hote_executeur":
                kwargs[param] = principal.host
            elif value == "agent_session":
                kwargs[param] = self._session_identity(principal, op, param)
            else:
                kwargs[param] = value
        if op.transport == "session/op":
            if not principal.agent:
                raise ScopeError("forbidden_scope", "jeton de session sans agent")
        else:
            if "B" in op.scope:
                if fence is None:   # déjà refusé par validate_request
                    raise ScopeError("bad_args", "enveloppe de bail absente")
                self._require_admitted(principal, fence.agent)
            if "A" in op.scope and op.agent_param:
                value = kwargs.get(op.agent_param)
                if not ("agregat" in op.scope and value == "all"):
                    self._require_admitted(principal, value)
        rule = getattr(self, "_apply_" + op.name.replace(".", "_"), None)
        if rule is not None:
            rule(principal, kwargs, fence)
        return kwargs

    @staticmethod
    def _session_identity(principal: Principal, op: Operation, param: str) -> str:
        if not principal.agent:
            raise ScopeError("forbidden_scope", "jeton de session sans agent")
        # les actions nomment leurs membres `agent:<nom>` (0012, actions.propose)
        if op.domain == "actions" and param == "proposed_by":
            return "agent:%s" % principal.agent
        return principal.agent

    # règles propres à une opération (portées A, B, H, S) ------------------
    def _apply_agents_claimable(self, principal, kw, fence):
        admitted = self.admitted_agents(principal)
        names = kw.get("names")
        wanted = sorted(admitted if not names else set(names) & admitted)
        # une liste vide voudrait dire « tous les agents de l'hôte » au pilote
        if not wanted or not host_available(self.db, principal.host):
            raise Immediate([])
        kw["names"] = wanted

    def _apply_agents_cwd_used(self, principal, kw, fence):
        # réponse limitée aux agents de l'hôte : le pilote regarde tout le
        # registre, la règle restreint la même question à l'hôte
        rows = self.db.query(
            "SELECT 1 AS ok FROM agent_registry WHERE cwd = %s AND name <> %s AND host = %s"
            " LIMIT 1", (kw.get("cwd"), kw.get("exclude"), principal.host))
        raise Immediate(bool(rows))

    def _apply_agents_upsert(self, principal, kw, fence):
        for key, value in kw.items():
            if key not in ("name", "cwd") and value is not None:
                raise ScopeError("bad_args", "agents.upsert : seul cwd est admis à distance")
        if not kw.get("cwd"):
            raise ScopeError("bad_args", "agents.upsert : cwd attendu")

    def _apply_leases_claim(self, principal, kw, fence):
        prefix = "exec:%s:" % principal.executor_id
        if not str(kw.get("owner") or "").startswith(prefix):
            raise ScopeError("forbidden_scope", "owner non préfixé par l'exécuteur authentifié")
        self._bound_ttl(kw)
        if not host_available(self.db, principal.host):
            raise ScopeError("host_unavailable", "hôte indisponible")

    def _apply_leases_renew(self, principal, kw, fence):
        self._bound_ttl(kw)

    def _bound_ttl(self, kw):
        ttl = float(kw.get("ttl_seconds") or 0)
        if ttl <= 0:
            raise ScopeError("bad_args", "ttl_seconds strictement positif attendu")
        kw["ttl_seconds"] = min(ttl, self.lease_ttl_s)

    def _apply_mailbox_unread(self, principal, kw, fence):
        kw["limit"] = self._bound_limit(kw.get("limit"))

    _apply_mailbox_unread_urgent = _apply_mailbox_unread

    def _apply_mailbox_reserve(self, principal, kw, fence):
        self._owner_epoch(kw)
        kw["limit"] = self._bound_limit(kw.get("limit"))
        self._bound_ids(kw.get("ids"))

    def _apply_mailbox_deliver(self, principal, kw, fence):
        self._owner_epoch(kw)
        self._bound_ids(kw.get("ids"))

    def _apply_mailbox_release(self, principal, kw, fence):
        self._bound_ids(kw.get("ids"))

    def _apply_operations_apply_restart(self, principal, kw, fence):
        self._owner_epoch(kw)

    @staticmethod
    def _owner_epoch(kw):
        if kw.get("owner") is None or kw.get("epoch") is None:
            raise ScopeError("bad_args", "owner et epoch non nuls à distance")

    @staticmethod
    def _bound_limit(limit) -> int:
        if limit is None:
            return MAX_LIMIT
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ScopeError("bad_args", "limit : entier positif attendu")
        return min(limit, MAX_LIMIT)

    @staticmethod
    def _bound_ids(ids):
        if ids is not None and len(ids) > MAX_IDS:
            raise ScopeError("bad_args", "trop d'identifiants (au plus %d)" % MAX_IDS)

    def _apply_turn_costs_last_reading(self, principal, kw, fence):
        if kw.get("agent") is None:
            raise ScopeError("bad_args", "agent=null refusé à distance")

    def _apply_operations_message_lots(self, principal, kw, fence):
        ids = self._int_ids(kw.get("ids"))
        kw["ids"] = self._own_messages(ids, self.admitted_agents(principal))
        if not kw["ids"]:
            raise Immediate([])

    def _int_ids(self, ids) -> list:
        self._bound_ids(ids)
        try:
            return sorted({int(i) for i in ids or ()})
        except (TypeError, ValueError):
            raise ScopeError("bad_args", "identifiants entiers attendus") from None

    def _own_messages(self, ids, recipients) -> list:
        if not ids or not recipients:
            return []
        rows = self.db.query(
            "SELECT id FROM agent_mailbox WHERE id IN (%s) AND recipient IN (%s)"
            % (", ".join(["%s"] * len(ids)), ", ".join(["%s"] * len(recipients))),
            tuple(ids) + tuple(sorted(recipients)))
        return sorted(int(r["id"]) for r in rows)

    def _apply_operations_record_gauges(self, principal, kw, fence):
        readings = kw.get("readings") or []
        if len(readings) > MAX_READINGS:
            raise ScopeError("bad_args", "trop de relevés (au plus %d)" % MAX_READINGS)
        admitted = self.admitted_agents(principal)
        if not admitted:
            raise ScopeError("forbidden_scope", "aucun agent admis sur l'hôte")
        rows = self.db.query(
            "SELECT DISTINCT harness, session_account FROM agent_registry WHERE name IN (%s)"
            % ", ".join(["%s"] * len(admitted)), tuple(sorted(admitted)))
        harnesses = {r["harness"] for r in rows}
        accounts = {(r["harness"], r["session_account"]) for r in rows if r["session_account"]}
        for g in readings:
            if not isinstance(g, Mapping) or g.get("harness") not in harnesses:
                raise ScopeError("forbidden_scope", "jauge d'un harnais hors de l'hôte")
            account = g.get("account")
            if account is None or (g["harness"], account) in accounts:
                continue
            active = self.db.query(
                "SELECT 1 AS ok FROM account_active WHERE host = %s AND harness = %s"
                " AND account = %s", (principal.host, g["harness"], account))
            if not active:
                raise ScopeError("forbidden_scope", "jauge d'un compte hors de l'hôte")

    def _apply_turn_resources_open_turn(self, principal, kw, fence):
        if kw.get("agent") != fence.agent:
            raise ScopeError("bad_args", "agent ≠ agent de l'enveloppe de bail")

    def _apply_turn_resources_close_turn(self, principal, kw, fence):
        rows = self.db.query(
            "SELECT agent, host FROM turn_resources WHERE turn_id = %s", (kw.get("turn_id"),))
        if rows and (rows[0]["agent"] != fence.agent or rows[0]["host"] != principal.host):
            raise ScopeError("forbidden_scope", "tour d'un autre agent")

    def _apply_hosts_record(self, principal, kw, fence):
        reading = kw.get("reading")
        if not isinstance(reading, Mapping):
            raise ScopeError("bad_args", "reading : objet attendu")
        kw["reading"] = dict(reading, host=principal.host)

    def _apply_work_mark_delegate_turn(self, principal, kw, fence):
        self._bound_ids(kw.get("item_ids"))

    # portée S (jeton de session) ------------------------------------------
    def _apply_leases_state(self, principal, kw, fence):
        if kw.get("name") != principal.agent:
            raise ScopeError("forbidden_scope", "seul l'agent du jeton de session")

    def _apply_mailbox_send(self, principal, kw, fence):
        if not self.db.query("SELECT 1 AS ok FROM agent_registry WHERE name = %s",
                             (kw.get("recipient"),)):
            raise ScopeError("bad_args", "destinataire inconnu (pas de création implicite)")

    def _apply_mailbox_mark_delivered(self, principal, kw, fence):
        ids = self._int_ids(kw.get("ids"))
        kw["ids"] = self._own_messages(ids, {principal.agent})
        if not kw["ids"]:
            raise Immediate(0)

    def _apply_work_move(self, principal, kw, fence):
        self._require_assigned(principal, kw.get("item_id"))

    _apply_work_note = _apply_work_move

    def _require_assigned(self, principal, item_id):
        rows = self.db.query(
            "SELECT 1 AS ok FROM work_items w WHERE w.id = %s AND (w.assignee = %s"
            " OR EXISTS (SELECT 1 FROM work_item_delegations d WHERE d.work_item_id = w.id"
            "            AND d.outcome IS NULL AND d.delegate = %s))",
            (int(item_id), principal.agent, principal.agent))
        if not rows:
            raise ScopeError("forbidden_scope", "lot non assigné à l'agent")

    # -- après l'appel --------------------------------------------------------
    def filter_result(self, principal: Principal, op: Operation, value: Any) -> Any:
        rule = getattr(self, "_filter_" + op.name.replace(".", "_"), None)
        return rule(principal, value) if rule is not None else value

    def _filter_agents_harnesses(self, principal, value):
        admitted = self.admitted_agents(principal)
        return {k: v for k, v in (value or {}).items() if k in admitted}

    def _filter_mailbox_get(self, principal, value):
        if value and value.get("recipient") in self.admitted_agents(principal):
            return value
        return None

    def _filter_budgets_limits(self, principal, value):
        admitted = self.admitted_agents(principal)
        return [r for r in value or () if (r.get("scope") or "") in admitted
                or not r.get("scope")]

    def _filter_agents_overview(self, principal, value):
        # annuaire réduit : ni consigne, ni dossier, ni bail. `role` : l'équipe
        # de la fiche (le registre n'a pas de colonne « rôle »)
        return [{"name": r.get("name"), "role": r.get("team") or "", "status": r.get("status")}
                for r in value or ()]

    def _filter_work_get(self, principal, value):
        if not value:
            return None
        rows = self.db.query(
            "SELECT (w.assignee = %s"
            "        OR EXISTS (SELECT 1 FROM work_item_delegations d"
            "                    WHERE d.work_item_id = w.id AND d.outcome IS NULL"
            "                      AND d.delegate = %s)"
            "        OR coalesce(nullif(btrim(w.app), ''), nullif(btrim(w.workstream), ''))"
            "           = (" + PROJECT_SQL + ")) AS ok"
            "  FROM work_items w WHERE w.id = %s",
            (principal.agent, principal.agent, principal.agent, int(value["id"])))
        return value if rows and rows[0]["ok"] else None

    def _filter_actions_get(self, principal, value):
        if value and value.get("proposed_by") in (principal.agent, "agent:%s" % principal.agent):
            return value
        return None
