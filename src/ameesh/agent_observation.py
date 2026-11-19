# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only observation of an agent's admitted profile (contract E1b, #7636).

`observe()` returns the ``ameesh-agent-observation/1`` document: the admitted
profile (responsible human, canon and placement references, harness, model,
provider, credential mode, capabilities), the lease (owner, epoch, expiry,
turn in progress), the effective execution bounds (context, turn duration,
mail bound, grace, session policy) and the redacted billing envelope of the
agent's account. It also exposes the native turn/sandbox references that are
available at observation time.

The document is a **closed projection**: every field is listed by the builder,
and no `pending_prompt`, `current_prompt`, `election_brief`, working directory,
session path, credential value, key name, account path or raw registry row is
ever selected. Budget figures are amounts, never a credential. A missing fact
is null plus a stable code in `missing`; it is never permission, admission or
zero cost.

Host-local facts are used only when the observed agent runs on the observer's
host. Another host's account profiles and responsible policy are not readable
here: off-host, the budget is refused (`foreign_host`) and the admission is
`null` with `responsible_policy` in `missing`, rather than substituting the
observer's configuration.

Atomicity: the builder opens its own ``REPEATABLE READ, READ ONLY``
transaction (`db.transaction(isolation=…, read_only=True)`) and reads every
fact inside it. One snapshot serves the whole document, and the transaction
cannot write: even alias resolution (`ameesh_resolve_agent_name`) skips its
journal insert in a read-only transaction. The builder performs no claim,
renewal, reservation, settlement, dispatch or canon synchronization.

The public surface is `ameesh agent observe` (CLI) and
``GET /api/v1/agents/{agent}/observation``; both use this builder, so their
`--json` document and HTTP body cannot drift.
"""
from __future__ import annotations

from . import accounts, modules, placement, registry, sandbox_observation, storage

SCHEMA = "ameesh-agent-observation/1"

#: freshness window of a canon state for a NEW turn. The same 300 s window as
#: `placement.SANDBOX_CANON_MAX_AGE`; exported in the document as `max_age_s`
#: so a consumer can apply its own bound instead of guessing ours.
CANON_MAX_AGE_S = placement.SANDBOX_CANON_MAX_AGE

#: stable reason codes of the admission block. The list is closed in `/1`:
#: a new code is an additive value a consumer must read as "not proven
#: admitted", never as permission.
REASONS = (
    "mode_externe",
    "retired",
    "ephemeral_expired",
    "host_claims_closed",
    "handoff_fenced",
    "responsible_missing",
    "responsible_policy_unavailable",
    "canon_state_missing",
    "canon_state_not_ok",
    "canon_stale",
    "canon_ref_mismatch",
    "placement_missing",
    "placement_refused",
    "placement_profile_diverged",
    "module_missing",
    "claim_predicate",
)

#: budget unavailability/rejection codes (closed in `/1`)
BUDGET_REASONS = (
    "no_account_profile",
    "unknown_account_profile",
    "ambiguous_account_profile",
    "account_config_unreadable",
    "no_billing_account",
    "no_account_envelope",
    "foreign_host",
    "exhausted",
)


#: facts this observation cannot supply at observation time. Closed in `/1`;
#: an unknown value is read as "unavailable", never as permission or zero.
MISSING = (
    "account_envelope",
    "budget_headroom",
    "limits",
    "responsible_policy",
    "canon_state",
)


class Unknown(LookupError):
    """The observed agent does not exist (CLI exit 3, HTTP 404)."""


#: One SELECT for the agent, its canon state, its placement and its lease.
#: `at` is read once: every derived age and liveness flag uses the database
#: clock of the snapshot, never the observer's clock. The claim predicate is
#: the core's own `registry.canon_claim_predicate_sql`, not a reimplementation.
_READ_SQL = """
WITH at AS (SELECT clock_timestamp() AS ts),
r AS (
    SELECT agent_registry.*
      FROM agent_registry
     WHERE name = (SELECT ameesh_resolve_agent_name(%s, %s))
)
SELECT r.name, r.status, r.status_text,
       extract(epoch FROM r.status_since)::float8 AS status_since_ts,
       r.mode, r.stop_reason,
       (r.retired_at IS NOT NULL) AS retired,
       coalesce(r.ephemeral, false) AS ephemeral,
       extract(epoch FROM r.ephemeral_expires_at)::float8 AS ephemeral_expires_ts,
       (coalesce(r.ephemeral, false)
        AND r.ephemeral_expires_at IS NOT NULL
        AND r.ephemeral_expires_at <= at.ts) AS ephemeral_expired,
       r.responsible, r.host, r.harness, r.model, r.provider, r.credential_mode,
       r.capabilities, r.canon, r.canon_ref, r.session_id, r.session_harness,
       r.session_account,
       r.context_max_tokens, r.turn_max_seconds, r.turn_mail_max,
       r.turn_grace_seconds, r.session_policy, r.session_max_tokens,
       r.turn_reread_budget, r.session_reread_budget, r.compact_tokens,
       r.placement_ok, r.placement_ref, r.placement_profile,
       r.placement_diagnostic, r.releve,
       ameesh_placement_profile(r.host, r.harness, r.provider, r.model,
                                r.credential_mode) AS placement_profile_current,
       coalesce(ameesh_host_claims_open(r.host), false) AS host_claims_open,
       coalesce(ameesh_handoff_fenced(r.name), false) AS handoff_fenced,
       __GOVERNED__ AS governed,
       __CLAIM__ AS claim_predicate,
       s.status AS canon_status, s.last_good_commit,
       s.diagnostic AS canon_diagnostic,
       extract(epoch FROM s.checked_at)::float8 AS canon_checked_ts,
       CASE WHEN s.checked_at IS NULL THEN NULL
            ELSE extract(epoch FROM (at.ts - s.checked_at))::float8 END AS canon_age_s,
       r.lease_owner, r.lease_epoch,
       extract(epoch FROM r.lease_expires_at)::float8 AS lease_expires_ts,
       extract(epoch FROM r.lease_since)::float8 AS lease_since_ts,
       (r.lease_owner IS NOT NULL AND r.lease_expires_at IS NOT NULL
        AND r.lease_expires_at > at.ts) AS lease_live,
       (r.status = 'running' AND r.lease_owner IS NOT NULL
        AND r.lease_expires_at IS NOT NULL
        AND r.lease_expires_at > at.ts) AS turn_in_progress,
       extract(epoch FROM at.ts)::float8 AS observed_ts
  FROM r CROSS JOIN at
  LEFT JOIN canon_state s
    ON s.host = r.host AND s.canon = coalesce(r.canon, '')
""".replace("__GOVERNED__", registry.canon_governed_sql("r")).replace(
    "__CLAIM__", registry.canon_claim_predicate_sql("r"))


def _float(value):
    return None if value is None else float(value)


def _int(value):
    return None if value is None else int(value)


def _bool(value):
    return bool(value)


def _capabilities(value):
    if value is None:
        return []
    if isinstance(value, str):
        # psql renders a text[] as a Postgres array literal; the JSON driver
        # renders a list. Normalise the literal without inventing a value.
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            inner = text[1:-1].strip()
            return [] if not inner else [part.strip().strip('"')
                                         for part in inner.split(",")]
        return [text] if text else []
    return [str(item) for item in value]


def _ref_commit(canon_ref):
    """The commit pinned at the end of a canon ref (`...@<commit>`), or None."""
    text = str(canon_ref or "")
    if "@" not in text:
        return None
    commit = text.rsplit("@", 1)[1].strip()
    return commit or None


def _same_host(cfg, row: dict) -> bool:
    """Whether the observer's host is the agent's host.

    Host-local configuration (declared account profiles, the responsible
    policy) describes the observer's host, not another host's. It is used only
    on a same-host read; elsewhere those facts are reported unavailable.
    """
    host = getattr(cfg, "host", "") or ""
    return bool(host) and row.get("host") == host


def _admission(row: dict, *, responsible_required: bool | None, fresh: bool,
               ref_matches: bool) -> dict:
    """The closed admission verdict and its stable reasons.

    `claim_predicate` is the core's own condition (retired, host claims,
    handoff fence, canon state and placement). `admitted` adds the profile
    level: an executable agent, a non-expired ephemeral, the responsible when
    the host requires one, and a *fresh* canon whose pinned commit is the last
    good one.

    `responsible_required` is None when the observer host is not the agent's
    host: that policy is host-local and cannot be read from another host.
    Every other fact is row- or database-global, so a failed profile condition
    still answers `False`; when all of them pass the answer is `None`
    (unknown, never permission) with `responsible_policy_unavailable`.
    Scheduler-only conditions (host holds, tenant claimer) belong to the claim
    path and are not encoded here.
    """
    reasons = []
    if row["mode"] != "execute":
        reasons.append("mode_externe")
    if row["retired"]:
        reasons.append("retired")
    if row["ephemeral_expired"]:
        reasons.append("ephemeral_expired")
    if not row["host_claims_open"]:
        reasons.append("host_claims_closed")
    if row["handoff_fenced"] and row["releve"] not in ("election", "preference"):
        reasons.append("handoff_fenced")
    with_responsible = bool((row.get("responsible") or "").strip())
    if responsible_required is True and not with_responsible:
        reasons.append("responsible_missing")
    if row["governed"]:
        if row["canon_status"] is None:
            reasons.append("canon_state_missing")
        elif row["canon_status"] != "ok":
            reasons.append("canon_state_not_ok")
        if row["placement_ok"] is None:
            reasons.append("placement_missing")
        elif not row["placement_ok"]:
            # #7860: a module its project selects is not installed on this
            # host; the canon stays ok, only this agent is refused. Other
            # placement causes follow it in the diagnostic.
            diagnostic = row.get("placement_diagnostic") or ""
            if diagnostic.startswith(modules.MISSING_PREFIX):
                reasons.append("module_missing")
            if not diagnostic.startswith(modules.MISSING_PREFIX) or "; " in diagnostic:
                reasons.append("placement_refused")
        elif row["placement_profile"] != row["placement_profile_current"]:
            reasons.append("placement_profile_diverged")
        if row["canon_status"] == "ok" and not fresh:
            reasons.append("canon_stale")
        if row["canon_status"] == "ok" and not ref_matches:
            reasons.append("canon_ref_mismatch")
    profile_ok = (bool(row["claim_predicate"]) and row["mode"] == "execute"
                  and not row["ephemeral_expired"]
                  and (not row["governed"] or (fresh and ref_matches)))
    if responsible_required is True and not with_responsible:
        profile_ok = False
    if responsible_required is None:
        admitted = None if profile_ok else False
        if profile_ok:
            reasons.append("responsible_policy_unavailable")
    else:
        admitted = profile_ok
    if admitted is False and not reasons:
        reasons.append("claim_predicate")
    return {"admitted": admitted,
            "claim_predicate": bool(row["claim_predicate"]),
            "governed": bool(row["governed"]),
            "responsible_required": responsible_required,
            "reasons": reasons}


def _canon_block(row: dict, *, observed_ts: float) -> dict | None:
    if not row["governed"]:
        return None
    state = row["canon_status"]
    checked = _float(row["canon_checked_ts"])
    last_good = row["last_good_commit"]
    fresh = False
    if state is not None:
        fresh, _ = placement.canon_fresh(
            {"status": state, "checked_ts": checked, "last_good_commit": last_good},
            now=observed_ts, max_age=CANON_MAX_AGE_S)
    ref_commit = _ref_commit(row["canon_ref"])
    return {
        "name": row.get("canon") or "",
        "state": state,
        "last_good_commit": last_good,
        "checked_ts": checked,
        "age_s": _float(row["canon_age_s"]),
        "max_age_s": float(CANON_MAX_AGE_S),
        "fresh": bool(fresh),
        "ref_commit": ref_commit,
        "ref_matches": bool(ref_commit and last_good
                            and ref_commit == last_good),
        "diagnostic": row.get("canon_diagnostic") or None,
    }


def _placement_block(row: dict) -> dict:
    return {
        "ok": (None if row["placement_ok"] is None
               else bool(row["placement_ok"])),
        "ref": row.get("placement_ref") or None,
        "profile": row.get("placement_profile") or None,
        "current": row.get("placement_profile_current") or None,
        "profile_ok": (None if (row["placement_ok"] is None
                               and row["placement_profile"] is None)
                       else row.get("placement_profile")
                       == row.get("placement_profile_current")),
        "diagnostic": row.get("placement_diagnostic") or None,
    }


def _session_block(row: dict) -> dict | None:
    if not (row.get("session_id") or row.get("session_harness")
            or row.get("session_account")):
        return None
    return {"id": row.get("session_id") or None,
            "harness": row.get("session_harness") or None,
            "account": row.get("session_account") or None}


def _declared_limits(row: dict) -> dict:
    return {
        "context_max_tokens": _int(row.get("context_max_tokens")),
        "turn_max_seconds": _int(row.get("turn_max_seconds")),
        "turn_mail_max": _int(row.get("turn_mail_max")),
        "turn_grace_seconds": _int(row.get("turn_grace_seconds")),
        "session_policy": row.get("session_policy") or None,
        "session_max_tokens": _int(row.get("session_max_tokens")),
        "turn_reread_budget": _int(row.get("turn_reread_budget")),
        "session_reread_budget": _int(row.get("session_reread_budget")),
        "compact_tokens": _int(row.get("compact_tokens")),
    }


def _effective_limits(cfg, row: dict) -> dict:
    from . import exploitation, mechanics
    session = mechanics.session_block(cfg)
    return {
        "context_max_tokens": exploitation.effective_context_max(
            cfg, row, row.get("harness")),
        "turn_max_seconds": exploitation.effective_turn_max_seconds(cfg, row),
        "turn_mail_max": exploitation.effective_turn_mail_max(cfg, row),
        "turn_grace_seconds": exploitation.effective_turn_grace_seconds(
            cfg, row),
        "session_policy": exploitation.effective_policy(cfg, row),
        # `session_max_tokens` has no host-wide effective value: only the
        # per-agent setting exists, so the declared value is the effective
        # one (None = the harness default).
        "session_max_tokens": _int(row.get("session_max_tokens")),
        "turn_reread_budget": exploitation.effective_turn_reread_budget(
            cfg, row, session),
        "session_reread_budget": exploitation.effective_session_reread_budget(
            cfg, row),
        "compact_tokens": exploitation.effective_compact_tokens(row, session),
    }


def _limits_block(cfg, row: dict) -> dict:
    declared = _declared_limits(row)
    effective = _effective_limits(cfg, row) if _same_host(cfg, row) else None
    return {"host": row.get("host") or None,
            "available": effective is not None,
            "declared": declared,
            "effective": effective}


def _account_profile(cfg, row: dict, requested: str | None):
    """(profile, reason) of the account to observe, fail-closed.

    The caller may name a declared profile (its prepared binding). Otherwise
    the session's recorded account is used, then the single declared profile.
    No profile is selected by guessing: an unknown or ambiguous selection
    yields a stable reason and no envelope.
    """
    try:
        items = accounts.profiles(cfg, row.get("harness") or "")
    except (accounts.AccountError, OSError):
        return None, "account_config_unreadable"
    if requested is not None:
        profile = accounts.by_name(items, requested)
        return (profile, None) if profile is not None else (
            None, "unknown_account_profile")
    recorded = row.get("session_account") or ""
    if recorded:
        profile = accounts.by_name(items, recorded)
        if profile is not None:
            return profile, None
        return None, "unknown_account_profile"
    if not items:
        return None, "no_account_profile"
    defaults = [profile for profile in items if profile.default]
    if len(defaults) == 1:
        return defaults[0], None
    if len(items) == 1:
        return items[0], None
    return None, "ambiguous_account_profile"


def _empty_budget(account_profile, reason) -> dict:
    """The unavailable budget block: amounts stay null, never zero."""
    return {
        "account_profile": account_profile,
        "billing_account": None,
        "available": False,
        "reason": reason,
        "reserve_usd": None,
        "limit_usd": None,
        "settled_usd": None,
        "reserved_usd": None,
        "kept_usd": None,
        "used_usd": None,
        "remaining_usd": None,
        "admitted": None,
        "period": None,
        "timezone": None,
        "reset_ts": None,
    }


def _budget_block(db, cfg, row: dict, requested: str | None) -> tuple[dict, list]:
    """The redacted envelope of the agent's account, or an unavailable block.

    The declared account profiles are host-local: another host's `p1` may bill
    a different account. Off the agent's host the selection is refused with
    `foreign_host` rather than substituting the observer's account.
    """
    recorded = requested or row.get("session_account") or None
    if not _same_host(cfg, row):
        return _empty_budget(recorded, "foreign_host"), ["account_envelope"]
    profile, reason = _account_profile(cfg, row, requested)
    if profile is None:
        return _empty_budget(recorded, reason), ["account_envelope"]
    block = _empty_budget(profile.name, None)
    block["reserve_usd"] = float(profile.reserve_usd)
    account = getattr(profile, "billing_account", "") or ""
    if not account:
        block["reason"] = "no_billing_account"
        return block, ["account_envelope"]
    # A database failure is not a fact about the envelope: it propagates and
    # the whole observation fails closed (the transaction is already aborted,
    # so returning a partial document would be a lie).
    rows = storage.of(db).budgets.account_usage(account)
    if not rows:
        block["reason"] = "no_account_envelope"
        return block, ["account_envelope"]
    envelope = rows[0]
    settled = float(envelope.get("settled") or 0.0)
    reserved = float(envelope.get("reserved") or 0.0)
    kept = float(envelope.get("kept") or 0.0)
    used = settled + reserved + kept
    limit = float(envelope["usd"])
    remaining = max(0.0, limit - used)
    admitted = used + float(profile.reserve_usd) <= limit + 1e-9
    block.update({
        "available": True,
        "reason": None if admitted else "exhausted",
        "billing_account": account,
        "limit_usd": limit,
        "settled_usd": settled,
        "reserved_usd": reserved,
        "kept_usd": kept,
        "used_usd": used,
        "remaining_usd": remaining,
        "admitted": admitted,
        "period": envelope.get("period"),
        "timezone": envelope.get("tz"),
        "reset_ts": _float(envelope.get("reset_ts")),
    })
    if not admitted:
        return block, ["budget_headroom"]
    return block, []


def _pending_turn(db, row: dict) -> dict | None:
    marker = registry.pending_spend_get(db, row["name"])
    if marker is None:
        return None
    return {"turn_id": marker.get("turn_id") or None,
            "spend_key": marker.get("ledger_key") or None,
            "model": marker.get("model") or None,
            "work_item_id": (str(marker["work_item_id"])
                             if marker.get("work_item_id") is not None else None),
            "created_ts": _float(marker.get("created_ts")),
            "ledger_ambiguous": bool(marker.get("ledger_ambiguous"))}


def _sandbox(db, name: str, *, sees=None) -> dict | None:
    """The current sandbox turn, or None. A read failure propagates: inside
    the observation transaction it is not an absence, it is an outage."""
    return sandbox_observation.current_agents(db, [name], sees=sees).get(name)


def _build(tx, name: str, *, cfg, account_profile: str | None,
           sees=None) -> dict:
    rows = tx.query(_READ_SQL, (name, "agent_observation"))
    if not rows:
        raise Unknown("unknown agent: %s" % name)
    row = rows[0]
    observed_ts = float(row["observed_ts"])
    # The responsible policy is host-local: another host's value is unknown,
    # and a local `False` must not turn a remote admission into `True`.
    responsible_required = (bool(getattr(cfg, "responsible_required", False))
                            if _same_host(cfg, row) else None)
    canon = _canon_block(row, observed_ts=observed_ts)
    fresh = bool(canon["fresh"]) if canon is not None else False
    ref_matches = bool(canon["ref_matches"]) if canon is not None else False
    admission = _admission(row, responsible_required=responsible_required,
                           fresh=fresh, ref_matches=ref_matches)
    limits = _limits_block(cfg, row)
    budget, budget_missing = _budget_block(tx, cfg, row, account_profile)
    missing = list(budget_missing)
    if responsible_required is None:
        missing.append("responsible_policy")
    if not limits["available"]:
        missing.append("limits")
    if canon is not None and canon["state"] is None:
        missing.append("canon_state")
    native = {"turn": _pending_turn(tx, row),
              "sandbox": _sandbox(tx, row["name"], sees=sees)}
    return {
        "schema": SCHEMA,
        "observed_ts": observed_ts,
        "agent": {
            "name": row["name"],
            "status": row["status"],
            "status_text": row.get("status_text") or None,
            "status_since_ts": _float(row.get("status_since_ts")),
            "mode": row.get("mode") or "execute",
            "retired": bool(row["retired"]),
            "stop_reason": row.get("stop_reason") or None,
            "ephemeral": bool(row["ephemeral"]),
            "ephemeral_expires_ts": _float(row.get("ephemeral_expires_ts")),
        },
        "profile": {
            "responsible": row.get("responsible") or None,
            "host": row.get("host") or None,
            "harness": row.get("harness") or None,
            "model": row.get("model") or None,
            "provider": row.get("provider") or None,
            "credential_mode": row.get("credential_mode") or None,
            "capabilities": _capabilities(row.get("capabilities")),
            "canon": row.get("canon") or None,
            "canon_ref": row.get("canon_ref") or None,
            "session": _session_block(row),
        },
        "admission": admission,
        "canon": canon,
        "placement": _placement_block(row),
        "lease": {
            "owner": row.get("lease_owner") or None,
            "epoch": _int(row.get("lease_epoch")),
            "live": bool(row["lease_live"]),
            "expires_ts": _float(row.get("lease_expires_ts")),
            "since_ts": _float(row.get("lease_since_ts")),
            "turn_in_progress": bool(row["turn_in_progress"]),
        },
        "limits": limits,
        "budget": budget,
        "native": native,
        "missing": missing,
    }


def observe(db, name: str, *, cfg, account_profile: str | None = None,
            sees=None) -> dict:
    """The `ameesh-agent-observation/1` document of one agent.

    Opens a repeatable-read, read-only transaction for the whole document.
    Raises `Unknown` when the agent does not exist. `cfg` supplies the
    observer's host defaults and declared account profiles; it is never
    written, and it is used only when the agent runs on `cfg.host`.
    `account_profile` selects a declared profile to report; without it the
    session's recorded account, then the single declared profile, is used.
    """
    with db.transaction(isolation="REPEATABLE READ", read_only=True) as tx:
        return _build(tx, name, cfg=cfg, account_profile=account_profile,
                      sees=sees)
